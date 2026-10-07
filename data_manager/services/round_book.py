"""Per-strategy fill and round accounting (petrosa-data-manager#537).

Win-rate shrinkage, Kelly sizing, the cold-start limit and the keep/kill rules all consume the closed rounds
of a strategy, so every fill has to end up in exactly one of three places: a closed round, the open round, or an
explicit "unattributed" bucket with a reason. A strategy that shows fills and no rounds is then visible as a
cause (all entries, no exits attributed to it) instead of an empty statistic.

A round is one cycle of a ``(strategy_id, symbol)`` position: it opens with the first fill after the position was
flat and closes when the open quantity returns to zero (a flip closes the round and opens the next one). Fills
are matched first-in-first-out, as in the P&L calculator, so the realized figure of a round agrees with it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

FILL_EVENT_TYPES = frozenset({"filled", "partial_fill"})
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


class RoundBook:
    """Replays fills in time order into rounds and keeps the books of every strategy."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], _Book] = defaultdict(_Book)
        self.closed: list[ClosedRound] = []
        # strategy -> counters
        self._fills: dict[str, int] = defaultdict(int)
        self._entry: dict[str, int] = defaultdict(int)
        self._exit: dict[str, int] = defaultdict(int)
        self._closed_fills: dict[str, int] = defaultdict(int)
        self.unattributed: dict[str, int] = defaultdict(int)

    def apply(self, row: dict[str, Any]) -> None:
        """Account for one fill row (other event types are not fills and are ignored)."""
        if row.get("event_type") not in FILL_EVENT_TYPES:
            return
        strategy_id = str(row.get("strategy_id") or "").strip()
        if strategy_id.lower() in PLACEHOLDER_STRATEGIES:
            reason = "no_strategy_id" if not strategy_id else "placeholder_strategy_id"
            self._unattributed(reason)
            return
        side = str(row.get("side") or "").lower()
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

        book = self._books[(strategy_id, symbol)]
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
                    symbol=str(symbol),
                    opened_at=cycle.opened_at,
                    closed_at=when,
                    realized=cycle.realized,
                    fills=cycle.fills,
                )
            )
            self._closed_fills[strategy_id] += cycle.fills
            book.cycle = _Cycle(opened_at=when) if remaining > 0 else None

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
        for (strategy_id, _symbol), book in self._books.items():
            if book.cycle is not None and (
                book.cycle.fills > 0 or book.long or book.short
            ):
                open_rounds[strategy_id] += 1
                open_fills[strategy_id] += book.cycle.fills
        closed_by: dict[str, list[ClosedRound]] = defaultdict(list)
        for closed in self.closed:
            closed_by[closed.strategy_id].append(closed)

        strategies: dict[str, Any] = {}
        for strategy_id in sorted(self._fills):
            rounds = closed_by.get(strategy_id, [])
            recent = [r for r in rounds if r.closed_at >= since]
            holding = [r.holding_seconds for r in recent]
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
