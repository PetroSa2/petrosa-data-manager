"""FIFO P&L calculator (P4.1, petrosa_k8s#601).

Replays a stream of execution fills into per-(strategy_id, symbol)
position queues and produces realized + unrealized P&L numbers at both
strategy and portfolio scope.

Position model
--------------

A position is tracked per ``(strategy_id, symbol)`` with two FIFO lot
queues: ``long`` (buys that haven't been matched against a sell) and
``short`` (sells that haven't been matched against a buy). When a buy
arrives:

  * If the short queue is non-empty, the buy first *closes* short lots
    in FIFO order. The realized P&L for each matched portion is
    ``(short_lot_price - buy_price) * matched_qty``.
  * Any remaining buy quantity opens new long lots.

A sell mirrors this — closes long lots first (realizing
``(sell_price - long_lot_price) * matched_qty``), then opens short
lots with any remaining quantity.

Mark-to-market unrealized P&L on remaining open lots is
``(mark_price - lot_price) * lot_qty`` for long lots and
``(lot_price - mark_price) * lot_qty`` for short lots.

The calculator is intentionally side-effect-free: callers feed fills
and marks into it, read results, and can serialize/restore state if
they need to persist a running tally. The NATS publication of
delta-P&L events is the *caller's* responsibility; this module just
hands back the numbers.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

# Event types that count as fills for the realized-P&L calculation.
FILL_EVENT_TYPES = frozenset({"filled", "partial_fill"})
SIDE_BUY = "buy"
SIDE_SELL = "sell"


@dataclass
class _Lot:
    """A single open lot. Quantities are always non-negative."""

    qty: float
    price: float


@dataclass
class FillImpact:
    """The realized impact of one fill on a position.

    ``realized_pnl`` is the delta-realized for this fill (sum of all
    lot-matches it produced). ``opened_qty`` is what stayed open on
    the corresponding side after matching.
    """

    strategy_id: str
    symbol: str
    side: str
    realized_pnl: float
    opened_qty: float = 0.0


@dataclass
class _Position:
    long: deque[_Lot] = field(default_factory=deque)
    short: deque[_Lot] = field(default_factory=deque)
    realized: float = 0.0


@dataclass
class PnlBreakdown:
    """Realized + unrealized P&L at a single scope."""

    realized: float
    unrealized: float

    @property
    def total(self) -> float:
        return self.realized + self.unrealized


class PnlCalculator:
    """Computes P&L by replaying fills + marking open positions to market."""

    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], _Position] = defaultdict(_Position)
        # Latest known mark per symbol, updated by `set_mark`.
        self._marks: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Ingestion.
    # ------------------------------------------------------------------

    def apply_fill(self, fill: dict[str, Any]) -> FillImpact | None:
        """Apply one fill row and return its impact.

        Returns ``None`` when the row is not a usable fill (wrong type,
        missing required field, or zero-quantity).
        """
        if fill.get("event_type") not in FILL_EVENT_TYPES:
            return None
        side = (fill.get("side") or "").lower()
        if side not in (SIDE_BUY, SIDE_SELL):
            return None
        strategy_id = fill.get("strategy_id")
        symbol = fill.get("symbol")
        qty = _maybe_float(fill.get("fill_qty") or fill.get("qty"))
        price = _maybe_float(fill.get("price"))
        if not strategy_id or not symbol or qty is None or price is None:
            return None
        if qty <= 0 or price <= 0:
            return None

        position = self._positions[(strategy_id, symbol)]
        self._marks[symbol] = price  # latest traded price doubles as a mark
        realized_delta = 0.0
        remaining = qty

        if side == SIDE_BUY:
            # Closing-short side first.
            while remaining > 0 and position.short:
                lot = position.short[0]
                match_qty = min(lot.qty, remaining)
                realized_delta += (lot.price - price) * match_qty
                lot.qty -= match_qty
                remaining -= match_qty
                if lot.qty <= 0:
                    position.short.popleft()
            if remaining > 0:
                position.long.append(_Lot(qty=remaining, price=price))
        else:  # SIDE_SELL
            while remaining > 0 and position.long:
                lot = position.long[0]
                match_qty = min(lot.qty, remaining)
                realized_delta += (price - lot.price) * match_qty
                lot.qty -= match_qty
                remaining -= match_qty
                if lot.qty <= 0:
                    position.long.popleft()
            if remaining > 0:
                position.short.append(_Lot(qty=remaining, price=price))

        position.realized += realized_delta
        return FillImpact(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            realized_pnl=realized_delta,
            opened_qty=remaining,
        )

    def set_mark(self, symbol: str, mark_price: float) -> None:
        """Override the latest mark price for a symbol."""
        self._marks[symbol] = mark_price

    # ------------------------------------------------------------------
    # Queries.
    # ------------------------------------------------------------------

    def strategy_pnl(self, strategy_id: str) -> PnlBreakdown:
        realized = 0.0
        unrealized = 0.0
        for (sid, symbol), pos in self._positions.items():
            if sid != strategy_id:
                continue
            realized += pos.realized
            mark = self._marks.get(symbol)
            if mark is None:
                continue
            for lot in pos.long:
                unrealized += (mark - lot.price) * lot.qty
            for lot in pos.short:
                unrealized += (lot.price - mark) * lot.qty
        return PnlBreakdown(realized=realized, unrealized=unrealized)

    def portfolio_pnl(self) -> PnlBreakdown:
        realized = 0.0
        unrealized = 0.0
        for (_, symbol), pos in self._positions.items():
            realized += pos.realized
            mark = self._marks.get(symbol)
            if mark is None:
                continue
            for lot in pos.long:
                unrealized += (mark - lot.price) * lot.qty
            for lot in pos.short:
                unrealized += (lot.price - mark) * lot.qty
        return PnlBreakdown(realized=realized, unrealized=unrealized)

    def position_summary(self) -> list[dict[str, Any]]:
        """Snapshot of every open position. Useful for debugging."""
        out: list[dict[str, Any]] = []
        for (sid, symbol), pos in self._positions.items():
            long_qty = sum(lot.qty for lot in pos.long)
            short_qty = sum(lot.qty for lot in pos.short)
            if long_qty == 0 and short_qty == 0 and pos.realized == 0:
                continue
            out.append(
                {
                    "strategy_id": sid,
                    "symbol": symbol,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                    "realized": pos.realized,
                }
            )
        return out


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
