"""Per-strategy fill and round accounting (petrosa-data-manager#537).

Win-rate shrinkage, Kelly sizing, the cold-start limit and the keep/kill rules all consume the closed rounds
of a strategy, so every fill has to end up in exactly one of three places: a closed round, the open round, or an
explicit "unattributed" bucket with a reason. A strategy that shows fills and no rounds is then visible as a
cause (all entries, no exits attributed to it) instead of an empty statistic.

A round is one cycle of a ``(strategy_id, symbol, position_side)`` leg. The account is in hedge mode, where a BUY
can open a LONG or close a SHORT, so a fill that carries its ``position_side`` (``LONG`` or ``SHORT``, also read
from ``ps`` and from the stored ``payload``) is booked on that leg: BUY/SELL on a LONG is entry/exit, on a SHORT
exit/entry, and a leg never flips. A fill without it is netted BUY against SELL on one ``(strategy_id, symbol)``
book as before (a flip closes the round and opens the next one), and counted as ``position_side_unknown`` in the
report so it is visible how many rows were netted. A round opens with the first entry fill after the leg was flat
and closes when its open quantity returns to zero. Fills are matched first-in-first-out, as in the P&L
calculator, so the realized figure of a round agrees with it. On a leg, the excess of an exit over the open lots
is ignored, and an exit with no open lot at all is unattributed (``exit_without_entry``).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from data_manager.services.fill_side import legacy_exit_side, order_side

FILL_EVENT_TYPES = frozenset({"filled", "partial_fill"})
NET = "NET"  # the leg of fills netted without a position side
UNATTRIBUTED = "_unattributed"
#: Strategy ids that are placeholders, not strategies.
PLACEHOLDER_STRATEGIES = frozenset({"", "unknown", "none", "null"})


@dataclass
class _Lot:
    qty: float
    price: float


@dataclass
class _Cycle:
    opened_at: datetime
    realized: float = 0.0
    fills: int = 0
    entry_fills: int = 0
    exit_fills: int = 0


@dataclass
class ClosedRound:
    strategy_id: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    realized: float
    fills: int
    position_side: str = NET

    @property
    def holding_seconds(self) -> float:
        return max(0.0, (self.closed_at - self.opened_at).total_seconds())


@dataclass
class _Book:
    long: deque[_Lot] = field(default_factory=deque)
    short: deque[_Lot] = field(default_factory=deque)
    cycle: _Cycle | None = None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _when(row: dict[str, Any]) -> datetime | None:
    value = row.get("fill_time") or row.get("timestamp")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        epoch = float(value)
        return datetime.fromtimestamp(epoch / 1000 if epoch > 1e12 else epoch, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def position_side(row: dict[str, Any]) -> str:
    """``LONG`` or ``SHORT`` when the fill carries its hedge-mode position side, else ``NET``.

    Read from ``position_side`` or ``ps``, on the event or in its ``payload`` (non-canonical fields are stored
    there). ``BOTH`` (one-way mode) and absent values net.
    """
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for source in (row, payload):
        for key in ("position_side", "ps"):
            value = str(source.get(key) or "").strip().upper()
            if value in ("LONG", "SHORT"):
                return value
    return NET


def _has_position_side(row: dict[str, Any]) -> bool:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return any(
        str(source.get(key) or "").strip().upper() in ("LONG", "SHORT", "BOTH")
        for source in (row, payload)
        for key in ("position_side", "ps")
    )


class RoundBook:
    """Replays fills in time order into rounds and keeps the books of every strategy."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str, str], _Book] = defaultdict(_Book)
        self.closed: list[ClosedRound] = []
        # strategy -> counters
        self._fills: dict[str, int] = defaultdict(int)
        self._entry: dict[str, int] = defaultdict(int)
        self._exit: dict[str, int] = defaultdict(int)
        self._closed_fills: dict[str, int] = defaultdict(int)
        self.unattributed: dict[str, int] = defaultdict(int)
        # fills netted because they carry no position side (per strategy)
        self._side_unknown: dict[str, int] = defaultdict(int)
        # legacy exit rows (side LONG/SHORT) read as the closing order side (petrosa-data-manager#550)
        self._legacy_mapped: dict[str, int] = defaultdict(int)

    def apply(self, row: dict[str, Any]) -> None:
        """Account for one fill row (other event types are not fills and are ignored)."""
        if row.get("event_type") not in FILL_EVENT_TYPES:
            return
        strategy_id = str(row.get("strategy_id") or "").strip()
        if strategy_id.lower() in PLACEHOLDER_STRATEGIES:
            reason = "no_strategy_id" if not strategy_id else "placeholder_strategy_id"
            self._unattributed(reason)
            return
        side, mapped = order_side(row)
        if mapped:
            self._legacy_mapped[strategy_id] += 1
        symbol = row.get("symbol")
        qty = _number(row.get("fill_qty") or row.get("fill_quantity") or row.get("qty"))
        price = _number(row.get("fill_price") or row.get("price"))
        when = _when(row)
        if (
            side not in ("buy", "sell")
            or not symbol
            or not qty
            or not price
            or when is None
        ):
            self._unattributed("unusable_fill")
            return
        if qty <= 0 or price <= 0:
            self._unattributed("unusable_fill")
            return

        leg = position_side(row)
        legacy = legacy_exit_side(row)
        if leg == NET and legacy is not None:
            # A legacy exit names the leg it closes; use that leg when it has open lots, else net it
            # with the entries that carried no position side (every entry before petrosa-tradeengine#743).
            candidate = self._books.get((strategy_id, symbol, legacy[1]))
            if candidate is not None and (candidate.long or candidate.short):
                leg = legacy[1]
        book = self._books[(strategy_id, symbol, leg)]
        if leg == NET:
            self._apply_netted(book, strategy_id, str(symbol), side, qty, price, when)
            if not _has_position_side(row):
                self._side_unknown[strategy_id] += 1
        else:
            self._apply_leg(book, strategy_id, str(symbol), leg, side, qty, price, when)

    def _apply_netted(
        self,
        book: _Book,
        strategy_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        when: datetime,
    ) -> None:
        """One book per (strategy, symbol), BUY netted against SELL (a fill with no position side)."""
        self._fills[strategy_id] += 1
        if book.cycle is None:
            book.cycle = _Cycle(opened_at=when)
        cycle = book.cycle
        cycle.fills += 1
        opposite, same = (
            (book.short, book.long) if side == "buy" else (book.long, book.short)
        )
        remaining = qty
        matched = 0.0
        realized = 0.0
        while remaining > 0 and opposite:
            lot = opposite[0]
            take = min(lot.qty, remaining)
            realized += (
                (lot.price - price) * take
                if side == "buy"
                else (price - lot.price) * take
            )
            lot.qty -= take
            remaining -= take
            matched += take
            if lot.qty <= 0:
                opposite.popleft()
        cycle.realized += realized
        if matched > 0:
            cycle.exit_fills += 1
            self._exit[strategy_id] += 1
        else:
            cycle.entry_fills += 1
            self._entry[strategy_id] += 1
        if remaining > 0:
            same.append(_Lot(qty=remaining, price=price))
        if matched > 0 and not opposite:
            # The position went flat (or flipped): the round is closed at this fill.
            self.closed.append(
                ClosedRound(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    opened_at=cycle.opened_at,
                    closed_at=when,
                    realized=cycle.realized,
                    fills=cycle.fills,
                )
            )
            self._closed_fills[strategy_id] += cycle.fills
            book.cycle = _Cycle(opened_at=when) if remaining > 0 else None

    def _apply_leg(
        self,
        book: _Book,
        strategy_id: str,
        symbol: str,
        leg: str,
        side: str,
        qty: float,
        price: float,
        when: datetime,
    ) -> None:
        """One hedge-mode leg: BUY/SELL on a LONG is entry/exit, on a SHORT exit/entry; it never flips."""
        lots = book.long if leg == "LONG" else book.short
        entry_side = "buy" if leg == "LONG" else "sell"
        if side == entry_side:
            self._fills[strategy_id] += 1
            if book.cycle is None:
                book.cycle = _Cycle(opened_at=when)
            book.cycle.fills += 1
            book.cycle.entry_fills += 1
            self._entry[strategy_id] += 1
            lots.append(_Lot(qty=qty, price=price))
            return
        if not lots:
            self._unattributed("exit_without_entry")
            return
        self._fills[strategy_id] += 1
        cycle = book.cycle if book.cycle is not None else _Cycle(opened_at=when)
        book.cycle = cycle
        cycle.fills += 1
        cycle.exit_fills += 1
        self._exit[strategy_id] += 1
        remaining = qty
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(lot.qty, remaining)
            cycle.realized += (
                (price - lot.price) * take
                if leg == "LONG"
                else (lot.price - price) * take
            )
            lot.qty -= take
            remaining -= take
            if lot.qty <= 0:
                lots.popleft()
        if not lots:
            self.closed.append(
                ClosedRound(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    opened_at=cycle.opened_at,
                    closed_at=when,
                    realized=cycle.realized,
                    fills=cycle.fills,
                    position_side=leg,
                )
            )
            self._closed_fills[strategy_id] += cycle.fills
            book.cycle = None

    def _unattributed(self, reason: str) -> None:
        self.unattributed[reason] += 1

    # ------------------------------------------------------------------
    def report(
        self, *, now: datetime | None = None, window_days: float = 30.0
    ) -> dict[str, Any]:
        """Per-strategy fills, rounds, rate and holding time, plus the unattributed fills and a check."""
        now = now or datetime.now(UTC)
        since = now - timedelta(days=window_days)
        open_rounds: dict[str, int] = defaultdict(int)
        open_fills: dict[str, int] = defaultdict(int)
        oldest_open: dict[str, datetime] = {}
        for (strategy_id, _symbol, _leg), book in self._books.items():
            if book.cycle is not None and (
                book.cycle.fills > 0 or book.long or book.short
            ):
                open_rounds[strategy_id] += 1
                open_fills[strategy_id] += book.cycle.fills
                opened = book.cycle.opened_at
                if strategy_id not in oldest_open or opened < oldest_open[strategy_id]:
                    oldest_open[strategy_id] = opened
        closed_by: dict[str, list[ClosedRound]] = defaultdict(list)
        for closed in self.closed:
            closed_by[closed.strategy_id].append(closed)

        strategies: dict[str, Any] = {}
        for strategy_id in sorted(self._fills):
            rounds = closed_by.get(strategy_id, [])
            recent = [r for r in rounds if r.closed_at >= since]
            holding = [r.holding_seconds for r in recent]
            started = [r.opened_at for r in rounds]
            if strategy_id in oldest_open:
                started.append(oldest_open[strategy_id])
            strategies[strategy_id] = {
                "fills": self._fills[strategy_id],
                "entry_fills": self._entry[strategy_id],
                "exit_fills": self._exit[strategy_id],
                "fills_in_closed_rounds": self._closed_fills[strategy_id],
                "fills_in_open_rounds": open_fills[strategy_id],
                "closed_rounds": len(rounds),
                "open_rounds": open_rounds[strategy_id],
                "window_days": window_days,
                "closed_rounds_in_window": len(recent),
                "closed_round_rate_per_day": len(recent) / window_days,
                "median_holding_seconds": median(holding) if holding else None,
                "n": len(recent),
                "realized_pnl_closed_rounds": sum(r.realized for r in rounds),
                # What the CIO needs for the win-rate posterior and the cold-start rules (petrosa-cio#297)
                "wins": sum(1 for r in rounds if r.realized > 0),
                "losses": sum(1 for r in rounds if r.realized < 0),
                "first_fill_at": min(started).isoformat() if started else None,
                "last_closed_at": (
                    max(r.closed_at for r in rounds).isoformat() if rounds else None
                ),
                "oldest_open_round_opened_at": (
                    oldest_open[strategy_id].isoformat()
                    if strategy_id in oldest_open
                    else None
                ),
                # fills netted BUY against SELL because they carry no position side
                "position_side_unknown": self._side_unknown[strategy_id],
                "legacy_exit_side_mapped": self._legacy_mapped[strategy_id],
            }
        attributed = sum(self._fills.values())
        unattributed = sum(self.unattributed.values())
        return {
            "strategies": strategies,
            "unattributed": dict(self.unattributed),
            "totals": {
                "fills": attributed + unattributed,
                "attributed_to_a_strategy": attributed,
                "unattributed": unattributed,
                "position_side_unknown": sum(self._side_unknown.values()),
                "legacy_exit_side_mapped": sum(self._legacy_mapped.values()),
            },
            # Every fill is in a closed round, the open round of its strategy, or unattributed.
            "accounted": all(
                s["fills"] == s["fills_in_closed_rounds"] + s["fills_in_open_rounds"]
                for s in strategies.values()
            ),
        }


def build_report(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_days: float = 30.0,
) -> dict[str, Any]:
    """Replay fill rows (any order) and return the per-strategy report."""
    book = RoundBook()
    ordered = sorted(
        (row for row in rows if row.get("event_type") in FILL_EVENT_TYPES),
        key=lambda row: _when(row) or datetime.min.replace(tzinfo=UTC),
    )
    for row in ordered:
        book.apply(row)
    return book.report(now=now, window_days=window_days)
