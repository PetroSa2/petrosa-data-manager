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
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from data_manager.services.fill_side import legacy_exit_side, order_side

FILL_EVENT_TYPES = frozenset({"filled", "partial_fill"})
NET = "NET"  # the leg of fills netted without a position side
UNATTRIBUTED = "_unattributed"
#: Strategy ids that are placeholders, not strategies.
PLACEHOLDER_STRATEGIES = frozenset({"", "unknown", "none", "null"})
ROUND_ORPHAN_AGE_FACTOR = 3.0
ROUND_ORPHAN_FALLBACK_HOLD_SECONDS = 4.0 * 60 * 60
ROUND_ORPHAN_MAX_SNAPSHOT_AGE_SECONDS = 1800.0
ROUND_ORPHAN_QUANTITY_TOLERANCE = 1e-12


@dataclass
class _Lot:
    qty: float
    price: float
    opened_at: datetime
    order_id: str | None = None
    position_id: str | None = None


ZERO = Decimal("0")
QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "FDUSD", "USD")


def _dec(value: Any) -> Decimal:
    """A Decimal of a number as written (``str`` of a float), ZERO when unusable."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


@dataclass
class _Cycle:
    opened_at: datetime
    realized: float = 0.0
    fills: int = 0
    entry_fills: int = 0
    exit_fills: int = 0
    # Decimal accounting of the round, for the cost-aware scorecard (petrosa-data-manager#468)
    realized_dec: Decimal = ZERO
    fees: Decimal = ZERO  # fees of the round's fills in the quote asset
    fee_unknown_fills: int = 0  # fills whose fee is missing or not in the quote asset
    entry_notional: Decimal = ZERO  # sum of entry quantity x price
    position_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)


@dataclass
class ClosedRound:
    strategy_id: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    realized: float
    fills: int
    position_side: str = NET
    realized_dec: Decimal = ZERO
    fees: Decimal = ZERO
    fee_unknown_fills: int = 0
    entry_notional: Decimal = ZERO
    position_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()

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

        self._row = row
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

    def _lot(self, qty: float, price: float, when: datetime) -> _Lot:
        row = getattr(self, "_row", {})
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        def field_of(name: str) -> Any:
            return row.get(name) if row.get(name) is not None else payload.get(name)

        order_id = field_of("order_id")
        position_id = field_of("position_id")
        return _Lot(
            qty=qty,
            price=price,
            opened_at=when,
            order_id=str(order_id) if order_id is not None else None,
            position_id=str(position_id) if position_id is not None else None,
        )

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
        self._tag(cycle)
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
            cycle.realized_dec += (
                (_dec(lot.price) - _dec(price)) * _dec(take)
                if side == "buy"
                else (_dec(price) - _dec(lot.price)) * _dec(take)
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
        flipped = matched > 0 and not opposite and remaining > 0
        if remaining > 0:
            same.append(self._lot(remaining, price, when))
            if not flipped:
                cycle.entry_notional += _dec(remaining) * _dec(price)
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
                    realized_dec=cycle.realized_dec,
                    fees=cycle.fees,
                    fee_unknown_fills=cycle.fee_unknown_fills,
                    entry_notional=cycle.entry_notional,
                    position_ids=tuple(cycle.position_ids),
                    decision_ids=tuple(cycle.decision_ids),
                )
            )
            self._closed_fills[strategy_id] += cycle.fills
            if remaining > 0:
                book.cycle = _Cycle(opened_at=when)
                book.cycle.entry_notional = _dec(remaining) * _dec(price)
            else:
                book.cycle = None

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
            self._tag(book.cycle)
            book.cycle.entry_notional += _dec(qty) * _dec(price)
            self._entry[strategy_id] += 1
            lots.append(self._lot(qty, price, when))
            return
        if not lots:
            self._unattributed("exit_without_entry")
            return
        self._fills[strategy_id] += 1
        cycle = book.cycle if book.cycle is not None else _Cycle(opened_at=when)
        book.cycle = cycle
        cycle.fills += 1
        cycle.exit_fills += 1
        self._tag(cycle)
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
            cycle.realized_dec += (
                (_dec(price) - _dec(lot.price)) * _dec(take)
                if leg == "LONG"
                else (_dec(lot.price) - _dec(price)) * _dec(take)
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
                    realized_dec=cycle.realized_dec,
                    fees=cycle.fees,
                    fee_unknown_fills=cycle.fee_unknown_fills,
                    entry_notional=cycle.entry_notional,
                    position_ids=tuple(cycle.position_ids),
                    decision_ids=tuple(cycle.decision_ids),
                )
            )
            self._closed_fills[strategy_id] += cycle.fills
            book.cycle = None

    def _tag(self, cycle: _Cycle) -> None:
        """Book the current fill's fee and identifiers on the round it belongs to."""
        row = getattr(self, "_row", None) or {}
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        def field_of(name: str) -> Any:
            return row.get(name) if row.get(name) is not None else payload.get(name)

        fee = field_of("fee")
        if fee is None:
            fee = field_of("fees")
        asset = str(field_of("fee_asset") or "").upper()
        status = str(field_of("fee_status") or "").lower()
        symbol = str(row.get("symbol") or "")
        quote = next((q for q in QUOTE_ASSETS if symbol.endswith(q)), "")
        in_quote = bool(asset) and asset == quote or (not asset and bool(quote))
        if fee is None or status in ("unknown", "needs_conversion") or not in_quote:
            cycle.fee_unknown_fills += 1
        else:
            cycle.fees += abs(_dec(fee))
        for name, bucket in (
            ("position_id", cycle.position_ids),
            ("decision_id", cycle.decision_ids),
        ):
            value = field_of(name)
            if value and str(value) not in bucket:
                bucket.append(str(value))

    def _unattributed(self, reason: str) -> None:
        self.unattributed[reason] += 1

    # ------------------------------------------------------------------
    def _orphan_overlay(
        self,
        *,
        now: datetime,
        window_days: float,
        exchange: dict[str, Any] | None,
        closed_entry_orders: set[str] | None,
    ) -> tuple[dict[tuple[str, str, str], dict[str, Any]], dict[str, Any]]:
        if exchange is None or closed_entry_orders is None:
            return {}, {}
        as_of_ms = exchange.get("as_of_ms")
        if as_of_ms is None:
            return {}, {"orphan_overlay": "missing", "exchange_snapshot": "missing"}
        snapshot_at = datetime.fromtimestamp(float(as_of_ms) / 1000, tz=UTC)
        age = max(0.0, (now - snapshot_at).total_seconds())
        if age > ROUND_ORPHAN_MAX_SNAPSHOT_AGE_SECONDS:
            return {}, {
                "orphan_overlay": "disabled_stale",
                "exchange_snapshot": "stale",
                "exchange_snapshot_age_seconds": age,
                "as_of_ms": as_of_ms,
            }
        rows = exchange.get("rows") or []
        exchange_by_leg: dict[tuple[str, str], float] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            side = str(row.get("position_side") or "").upper()
            quantity = _number(row.get("quantity")) or 0.0
            if side == "BOTH":
                side = "LONG" if quantity >= 0 else "SHORT"
            if side in ("LONG", "SHORT"):
                exchange_by_leg[(symbol, side)] = abs(quantity)

        closed_by: dict[str, list[ClosedRound]] = defaultdict(list)
        for closed in self.closed:
            if closed.closed_at >= now - timedelta(days=window_days):
                closed_by[closed.strategy_id].append(closed)
        thresholds = {
            strategy: (
                ROUND_ORPHAN_AGE_FACTOR * median(r.holding_seconds for r in rounds),
                "median",
            )
            if rounds
            else (ROUND_ORPHAN_FALLBACK_HOLD_SECONDS, "fallback")
            for strategy, rounds in closed_by.items()
        }
        for strategy in self._fills:
            thresholds.setdefault(
                strategy, (ROUND_ORPHAN_FALLBACK_HOLD_SECONDS, "fallback")
            )

        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        legs: dict[tuple[str, str], list[tuple[str, _Lot]]] = defaultdict(list)
        for (strategy, symbol, book_leg), book in self._books.items():
            candidates = []
            if book_leg == NET:
                if book.long:
                    candidates.append(("LONG", book.long))
                if book.short:
                    candidates.append(("SHORT", book.short))
            else:
                candidates.append(
                    (book_leg, book.long if book_leg == "LONG" else book.short)
                )
            for leg, lots in candidates:
                for lot in lots:
                    legs[(symbol, leg)].append((strategy, lot))

        for (symbol, leg), entries in legs.items():
            exchange_quantity = exchange_by_leg.get((symbol, leg), 0.0)
            remaining_exchange = exchange_quantity
            leg_total = sum(lot.qty for _, lot in entries)
            ordered = sorted(entries, key=lambda item: item[1].opened_at, reverse=True)
            for strategy, lot in ordered:
                key = (strategy, symbol, leg)
                bucket = result.setdefault(
                    key,
                    {
                        "orphaned": [],
                        "ledger_closed": [],
                        "open_lot_quantity": 0.0,
                        "held_quantity": 0.0,
                        "orphaned_quantity": 0.0,
                        "ledger_closed_quantity": 0.0,
                        "exchange_quantity": exchange_quantity,
                        "unowned_exchange_quantity": max(
                            exchange_quantity - leg_total, 0.0
                        ),
                        "as_of_ms": as_of_ms,
                        "exchange_snapshot_age_seconds": age,
                        "orphan_threshold_seconds": thresholds[strategy][0],
                        "holding_source": thresholds[strategy][1],
                        "_orphaned_lot_ids": set(),
                        "_ledger_closed_lot_ids": set(),
                    },
                )
                if lot.order_id in closed_entry_orders:
                    bucket["ledger_closed"].append(
                        self._lot_dict(lot, lot.qty, "ledger_closed")
                    )
                    bucket["ledger_closed_quantity"] += lot.qty
                    bucket["_ledger_closed_lot_ids"].add(id(lot))
                    bucket["open_lot_quantity"] += lot.qty
                    continue
                bucket["open_lot_quantity"] += lot.qty
                if lot.opened_at.timestamp() * 1000 > float(as_of_ms):
                    bucket["held_quantity"] += lot.qty
                    continue
                held = min(lot.qty, remaining_exchange)
                remaining_exchange -= held
                excess = lot.qty - held
                threshold = thresholds[strategy][0]
                eligible = (now - lot.opened_at).total_seconds() >= threshold
                bucket["held_quantity"] += held
                if excess > ROUND_ORPHAN_QUANTITY_TOLERANCE and eligible:
                    reason = (
                        "exchange_flat"
                        if exchange_quantity == 0
                        else "exchange_quantity_excess"
                    )
                    bucket["orphaned"].append(self._lot_dict(lot, excess, reason))
                    bucket["orphaned_quantity"] += excess
                    bucket["_orphaned_lot_ids"].add(id(lot))
                else:
                    bucket["held_quantity"] += excess
            for strategy, lot in entries:
                result.setdefault(
                    (strategy, symbol, leg),
                    {
                        "orphaned": [],
                        "ledger_closed": [],
                        "open_lot_quantity": 0.0,
                        "held_quantity": 0.0,
                        "orphaned_quantity": 0.0,
                        "ledger_closed_quantity": 0.0,
                        "exchange_quantity": exchange_quantity,
                        "unowned_exchange_quantity": max(
                            exchange_quantity - leg_total, 0.0
                        ),
                        "as_of_ms": as_of_ms,
                        "exchange_snapshot_age_seconds": age,
                        "orphan_threshold_seconds": thresholds[strategy][0],
                        "holding_source": thresholds[strategy][1],
                        "_orphaned_lot_ids": set(),
                        "_ledger_closed_lot_ids": set(),
                    },
                )
        return result, {
            "orphan_overlay": "enabled",
            "exchange_snapshot": "fresh",
            "exchange_snapshot_age_seconds": age,
            "as_of_ms": as_of_ms,
        }

    @staticmethod
    def _lot_dict(lot: _Lot, quantity: float, reason: str) -> dict[str, Any]:
        return {
            "opened_at": lot.opened_at.isoformat(),
            "quantity": quantity,
            "price": lot.price,
            "order_id": lot.order_id,
            "position_id": lot.position_id,
            "reason": reason,
        }

    def report(
        self,
        *,
        now: datetime | None = None,
        window_days: float = 30.0,
        exchange: dict[str, Any] | None = None,
        closed_entry_orders: set[str] | None = None,
        apply_overlay: bool = False,
    ) -> dict[str, Any]:
        """Per-strategy fills, rounds, rate and holding time, plus the unattributed fills and a check."""
        now = now or datetime.now(UTC)
        since = now - timedelta(days=window_days)
        overlay, overlay_meta = self._orphan_overlay(
            now=now,
            window_days=window_days,
            exchange=exchange,
            closed_entry_orders=closed_entry_orders,
        )
        overlay_enabled = exchange is not None and closed_entry_orders is not None
        open_rounds: dict[str, int] = defaultdict(int)
        open_fills: dict[str, int] = defaultdict(int)
        oldest_open: dict[str, datetime] = {}
        for (strategy_id, _symbol, _leg), book in self._books.items():
            surviving = []
            for leg, lots in (("LONG", book.long), ("SHORT", book.short)):
                bucket = overlay.get((strategy_id, _symbol, leg))
                if apply_overlay and bucket:
                    orphaned = bucket["_orphaned_lot_ids"]
                    closed = bucket["_ledger_closed_lot_ids"]
                    surviving.extend(
                        lot
                        for lot in lots
                        if id(lot) not in closed and id(lot) not in orphaned
                    )
                else:
                    surviving.extend(lots)
            overlay_applied = apply_overlay and any(
                (strategy_id, _symbol, leg) in overlay for leg in ("LONG", "SHORT")
            )
            if book.cycle is not None and (
                bool(surviving)
                if overlay_applied
                else book.cycle.fills > 0 or bool(surviving)
            ):
                open_rounds[strategy_id] += 1
                open_fills[strategy_id] += book.cycle.fills
                opened = (
                    min(
                        (lot.opened_at for lot in surviving),
                        default=book.cycle.opened_at,
                    )
                    if overlay_applied
                    else book.cycle.opened_at
                )
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
            strategy = {
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
            if overlay_enabled:
                strategy_legs: dict[str, dict[str, Any]] = defaultdict(dict)
                for (owner, symbol, leg), data in overlay.items():
                    if owner == strategy_id:
                        public_data = {
                            key: value
                            for key, value in data.items()
                            if not key.startswith("_")
                        }
                        strategy_legs[symbol][leg] = public_data
                leg_values = [
                    data
                    for symbol_data in strategy_legs.values()
                    for data in symbol_data.values()
                ]
                strategy["orphaned_rounds"] = sum(
                    bool(v["orphaned"]) for v in leg_values
                )
                strategy["orphaned_lots"] = sum(len(v["orphaned"]) for v in leg_values)
                strategy["orphaned_quantity"] = sum(
                    v["orphaned_quantity"] for v in leg_values
                )
                strategy["ledger_closed_rounds"] = sum(
                    bool(v["ledger_closed"]) for v in leg_values
                )
                strategy["oldest_orphaned_lot_opened_at"] = min(
                    (item["opened_at"] for v in leg_values for item in v["orphaned"]),
                    default=None,
                )
                strategy["orphan_threshold_seconds"] = next(
                    (v["orphan_threshold_seconds"] for v in leg_values),
                    ROUND_ORPHAN_FALLBACK_HOLD_SECONDS,
                )
                strategy["holding_source"] = next(
                    (v["holding_source"] for v in leg_values), "fallback"
                )
                strategy["legs"] = strategy_legs
            if apply_overlay and overlay_enabled:
                strategy["open_rounds"] = sum(
                    1
                    for symbol_data in strategy_legs.values()
                    for v in symbol_data.values()
                    if v["held_quantity"] > 0
                )
                strategy["oldest_open_round_opened_at"] = (
                    oldest_open[strategy_id].isoformat()
                    if strategy_id in oldest_open
                    else None
                )
            strategies[strategy_id] = strategy
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
            **(overlay_meta if overlay_enabled else {}),
        }


def build_report(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_days: float = 30.0,
    exchange: dict[str, Any] | None = None,
    closed_entry_orders: set[str] | None = None,
    apply_overlay: bool = False,
) -> dict[str, Any]:
    """Replay fill rows (any order) and return the per-strategy report."""
    book = RoundBook()
    ordered = sorted(
        (row for row in rows if row.get("event_type") in FILL_EVENT_TYPES),
        key=lambda row: _when(row) or datetime.min.replace(tzinfo=UTC),
    )
    for row in ordered:
        book.apply(row)
    return book.report(
        now=now,
        window_days=window_days,
        exchange=exchange,
        closed_entry_orders=closed_entry_orders,
        apply_overlay=apply_overlay,
    )
