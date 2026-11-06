"""Per-strategy closed rounds and realized P&L rebuilt from exchange fills (dm#844).

The reference both outcome calculators (netting, and the leg-aware replay of dm#842)
are judged against. Inputs are the account's own fills and orders (Binance
userTrades/allOrders, read through tradeengine), tradeengine's strategy position rows
and its order->strategy map. Pure: no I/O, so it is tested on recorded fills.

A round is one strategy position row:

- **entry**: the fills of its ``entry_order_id``;
- **exit**: the fills of its known exit orders (``exit_order_id``, ``tp_order_id``,
  ``sl_order_id``, and exit orders the execution journal ties to the row). A row that
  closed with no known exit order (``closed_externally``) gets the closing-side fills
  after its entry only when they add up exactly to its quantity and no other row shares
  the symbol and side over that time; otherwise it is listed unmatched with the reason.

Round P&L is computed from the round's own fill prices: gross = (exit VWAP - entry
VWAP) x quantity (reversed for SHORT), fees = the entry fills' commission pro rata plus
the exit fills' commission, net = gross - fees. Binance's ``realizedPnl`` of the exit
fills is reported alongside: the exchange computes it against the account's blended
entry price, so it differs from the round's own P&L whenever rows share a position.

Every fill ends up in a round or in ``unmatched_fills`` with a reason, and every closed
row ends up as a round or in ``unmatched_rounds`` with a reason. Nothing is dropped.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

CLOSED_STATUSES = frozenset({"closed", "closed_externally"})
_CLOSING_SIDE = {"LONG": "SELL", "SHORT": "BUY"}
_ENTRY_SIDE = {"LONG": "BUY", "SHORT": "SELL"}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    return None


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _vwap(fills: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    qty = sum(((_dec(f.get("qty")) or Decimal("0")) for f in fills), Decimal("0"))
    notional = sum(
        (
            (_dec(f.get("price")) or Decimal("0"))
            * (_dec(f.get("qty")) or Decimal("0"))
            for f in fills
        ),
        Decimal("0"),
    )
    return qty, (notional / qty if qty else Decimal("0"))


def _usdt_commission(fills: list[dict[str, Any]]) -> Decimal | None:
    total = Decimal("0")
    for fill in fills:
        amount = _dec(fill.get("commission")) or Decimal("0")
        if amount and str(fill.get("commissionAsset") or "USDT") != "USDT":
            return None
        total += amount
    return total


def _position_side(fill: dict[str, Any]) -> str:
    return str(fill.get("positionSide") or "BOTH").upper()


def _row_key(row: dict[str, Any]) -> str:
    return str(row.get("strategy_position_id") or row.get("position_id") or "")


def build_ground_truth(
    fills: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    order_strategy: dict[str, str],
    exit_orders_by_position: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Rebuild every strategy's closed rounds from the exchange's fills."""
    exit_orders_by_position = exit_orders_by_position or {}
    orders_by_id = {str(o.get("orderId")): o for o in orders}
    fills_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in sorted(
        fills, key=lambda f: (_ms(f.get("time")) or 0, f.get("id") or 0)
    ):
        fills_by_order[str(fill.get("orderId"))].append(fill)

    claimed: set[Any] = set()
    # Entry fills of a row that could not be closed out: theirs, not row-less.
    unmatched_entry: dict[Any, str] = {}
    rounds: list[dict[str, Any]] = []
    unmatched_rounds: list[dict[str, Any]] = []
    open_rows = 0
    ordered_rows = sorted(positions, key=lambda r: _ms(r.get("entry_time")) or 0)

    for row in ordered_rows:
        status = str(row.get("status") or "").lower()
        if status not in CLOSED_STATUSES:
            open_rows += 1
            continue
        side = str(row.get("side") or "").upper()
        base = {
            "strategy_position_id": _row_key(row),
            "strategy_id": row.get("strategy_id") or "unknown",
            "symbol": row.get("symbol"),
            "side": side,
            "status": status,
            "close_reason": row.get("close_reason"),
            "entry_time_ms": _ms(row.get("entry_time")),
        }
        if side not in _CLOSING_SIDE:
            unmatched_rounds.append({**base, "reason": "side_unknown"})
            continue
        entry_fills = [
            f
            for f in fills_by_order.get(str(row.get("entry_order_id")), [])
            if f.get("id") not in claimed
        ]
        if not entry_fills:
            unmatched_rounds.append({**base, "reason": "entry_fills_not_found"})
            continue
        entry_ids = {f.get("id") for f in entry_fills}

        exit_ids = {
            str(v)
            for v in (
                row.get("exit_order_id"),
                row.get("tp_order_id"),
                row.get("sl_order_id"),
            )
            if v
        } | set(exit_orders_by_position.get(_row_key(row), set()))
        exit_fills = [
            f
            for order_id in sorted(exit_ids)
            for f in fills_by_order.get(order_id, [])
            if f.get("id") not in claimed
            and str(f.get("side")).upper() == _CLOSING_SIDE[side]
        ]
        attribution = "exit_order"
        entry_qty, entry_price = _vwap(entry_fills)
        if not exit_fills:
            exit_fills, reason = _attribute_by_quantity(
                row, side, entry_qty, fills, claimed, ordered_rows
            )
            if reason is not None:
                for fill_id in entry_ids:
                    unmatched_entry[fill_id] = _row_key(row)
                unmatched_rounds.append(
                    {
                        **base,
                        "reason": reason,
                        "candidate_fill_ids": [f.get("id") for f in exit_fills],
                    }
                )
                continue
            attribution = "closing_fills_by_quantity"

        exit_qty, exit_price = _vwap(exit_fills)
        qty = min(entry_qty, exit_qty)
        gross = (
            (exit_price - entry_price) * qty
            if side == "LONG"
            else (entry_price - exit_price) * qty
        )
        entry_fee = _usdt_commission(entry_fills)
        exit_fee = _usdt_commission(exit_fills)
        fees = (
            None
            if entry_fee is None or exit_fee is None
            else entry_fee * (qty / entry_qty if entry_qty else Decimal("0")) + exit_fee
        )
        net = None if fees is None else gross - fees
        exchange_realized = sum(
            ((_dec(f.get("realizedPnl")) or Decimal("0")) for f in exit_fills),
            Decimal("0"),
        )
        close_types = sorted(
            {
                str(
                    orders_by_id.get(str(f.get("orderId")), {}).get("type") or "unknown"
                )
                for f in exit_fills
            }
        )
        for fill in entry_fills + exit_fills:
            claimed.add(fill.get("id"))
        rounds.append(
            {
                **base,
                "attribution": attribution,
                "quantity": _num(qty),
                "entry_quantity": _num(entry_qty),
                "exit_quantity": _num(exit_qty),
                "quantity_note": None
                if entry_qty == exit_qty
                else "entry_exit_qty_differ",
                "entry_price": _num(entry_price),
                "exit_price": _num(exit_price),
                "exit_time_ms": max(_ms(f.get("time")) or 0 for f in exit_fills),
                "gross_pnl": _num(gross),
                "fees": _num(fees),
                "net_pnl": _num(net),
                "exchange_realized_pnl": _num(exchange_realized),
                "exit_order_types": close_types,
                "entry_fill_ids": [f.get("id") for f in entry_fills],
                "exit_fill_ids": [f.get("id") for f in exit_fills],
            }
        )

    unmatched_fills = []
    for fill in fills:
        if fill.get("id") in claimed:
            continue
        order = orders_by_id.get(str(fill.get("orderId")), {})
        closing = bool(order.get("reduceOnly") or order.get("closePosition"))
        if fill.get("id") in unmatched_entry:
            reason = "entry_of_unmatched_round"
        elif closing:
            reason = "close_without_position_row"
        else:
            reason = "fill_without_position_row"
        unmatched_fills.append(
            {
                "fill_id": fill.get("id"),
                "order_id": fill.get("orderId"),
                "symbol": fill.get("symbol"),
                "side": fill.get("side"),
                "qty": fill.get("qty"),
                "price": fill.get("price"),
                "realized_pnl": fill.get("realizedPnl"),
                "time_ms": _ms(fill.get("time")),
                "order_type": order.get("type"),
                "strategy_id": order_strategy.get(str(fill.get("orderId"))),
                "strategy_position_id": unmatched_entry.get(fill.get("id")),
                "reason": reason,
            }
        )

    return {
        "strategies": _per_strategy(rounds, unmatched_rounds),
        "rounds": rounds,
        "unmatched_rounds": unmatched_rounds,
        "unmatched_fills": unmatched_fills,
        "totals": {
            "fills": len(fills),
            "fills_in_rounds": len(claimed),
            "fills_unmatched": len(unmatched_fills),
            "closed_rows": len(rounds) + len(unmatched_rounds),
            "rounds": len(rounds),
            "rounds_unmatched": len(unmatched_rounds),
            "open_rows": open_rows,
        },
    }


def _attribute_by_quantity(
    row: dict[str, Any],
    side: str,
    entry_qty: Decimal,
    fills: list[dict[str, Any]],
    claimed: set[Any],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Closing fills for a row with no known exit order; a reason when not provable."""
    symbol = row.get("symbol")
    entry_ms = _ms(row.get("entry_time")) or 0
    exit_ms = _ms(row.get("exit_time"))
    candidates = [
        f
        for f in fills
        if f.get("id") not in claimed
        and f.get("symbol") == symbol
        and str(f.get("side")).upper() == _CLOSING_SIDE[side]
        and _position_side(f) in ("BOTH", side)
        and (_ms(f.get("time")) or 0) >= entry_ms
        and (exit_ms is None or (_ms(f.get("time")) or 0) <= exit_ms)
    ]
    for other in rows:
        if _row_key(other) == _row_key(row):
            continue
        if (
            other.get("symbol") != symbol
            or str(other.get("side") or "").upper() != side
        ):
            continue
        other_in = _ms(other.get("entry_time")) or 0
        other_out = _ms(other.get("exit_time"))
        overlaps = other_in <= (exit_ms if exit_ms is not None else other_in) and (
            other_out is None or other_out >= entry_ms
        )
        if overlaps:
            return candidates, "symbol_side_shared"
    if not candidates:
        return [], "no_closing_fills"
    total = sum(
        ((_dec(f.get("qty")) or Decimal("0")) for f in candidates), Decimal("0")
    )
    if total != entry_qty:
        return candidates, "quantity_mismatch"
    return candidates, None


def _per_strategy(
    rounds: list[dict[str, Any]], unmatched_rounds: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rounds:
        by_strategy[str(item["strategy_id"])].append(item)
    for item in unmatched_rounds:
        by_strategy.setdefault(str(item["strategy_id"]), [])
    unmatched_count: dict[str, int] = defaultdict(int)
    for item in unmatched_rounds:
        unmatched_count[str(item["strategy_id"])] += 1

    for strategy, items in sorted(by_strategy.items()):
        items.sort(key=lambda r: r["exit_time_ms"])
        nets = [r["net_pnl"] for r in items]
        known = [n for n in nets if n is not None]
        streak = worst = 0
        for net in known:
            streak = streak + 1 if net < 0 else 0
            worst = max(worst, streak)
        out[strategy] = {
            "closed_rounds": len(items),
            "wins": sum(1 for n in known if n > 0),
            "losses": sum(1 for n in known if n < 0),
            "flat": sum(1 for n in known if n == 0),
            "net_unknown": len(nets) - len(known),
            "max_consecutive_losses": worst,
            "gross_pnl": sum(r["gross_pnl"] or 0.0 for r in items),
            "fees": sum(r["fees"] or 0.0 for r in items),
            "net_pnl": sum(known),
            "exchange_realized_pnl": sum(
                r["exchange_realized_pnl"] or 0.0 for r in items
            ),
            "unmatched_rounds": unmatched_count.get(strategy, 0),
        }
    return out
