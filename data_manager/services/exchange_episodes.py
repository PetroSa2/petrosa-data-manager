"""Per-strategy ground truth from the exchange's own flat-to-flat episodes (dm#844 v2).

The account's fills (Binance userTrades) are the spine; tradeengine's ledger rows are
only annotations, so stale or mislinked rows cannot block attribution.

**Episodes.** Fills are walked in time order per ``(symbol, positionSide)``:

- Hedge mode (``LONG`` / ``SHORT``): an opening fill (BUY for LONG, SELL for SHORT)
  adds to the position; a closing fill reduces it. An episode runs from flat to flat.
- One-way mode (``BOTH``): the net position is signed (BUY +, SELL -). An episode ends
  when the net crosses zero. A fill that flips the sign closes the episode with the
  part of its quantity that reaches zero and opens the next with the rest; its
  realized P&L and commission are split pro rata, and both parts are flagged
  ``split_fill``.

**Episode P&L** = sum of Binance ``realizedPnl`` - commission (USDT). Within one
episode the exchange's blended entry is the episode's own entry, so this is exact. A
commission charged in another asset makes ``net_pnl`` null; nothing is converted.

**Attribution.** An episode belongs to a strategy only when every opening fill maps to
that one strategy (order -> strategy from the execution journal and the ledger's entry
orders). Otherwise it is ``multi_strategy`` (with each strategy's share of the opening
quantity, never split into per-strategy P&L) or ``unmapped`` (an opening fill whose
order has no strategy).

**Edges of the window.** An episode whose first fill reduces a position (it was opened
before the window) is ``started_before_window``; one still open at the end is
``open_at_window_end``. Neither is counted in W/L.

**Closes** are classified by their order: ``stop_or_tp_trigger`` (STOP*, TAKE_PROFIT*,
TRAILING_STOP_MARKET), ``reduce_only_market`` (MARKET with reduceOnly/closePosition),
``liquidation`` / ``adl`` (Binance's ``autoclose-`` / ``adl_autoclose`` client ids),
``market`` and ``limit`` otherwise, ``unknown`` without the order.

Pure functions: no I/O, tested on recorded fills.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
_TRIGGER_TYPES = {
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ZERO


def _ms(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    return 0


def close_kind(order: dict[str, Any] | None) -> str:
    """How a closing fill's order closed the position."""
    if not order:
        return "unknown"
    client = str(order.get("clientOrderId") or "")
    if client.startswith("autoclose-"):
        return "liquidation"
    if client.startswith("adl_autoclose"):
        return "adl"
    order_type = str(order.get("origType") or order.get("type") or "").upper()
    if order_type in _TRIGGER_TYPES:
        return "stop_or_tp_trigger"
    if order_type == "MARKET":
        if order.get("reduceOnly") or order.get("closePosition"):
            return "reduce_only_market"
        return "market"
    if order_type == "LIMIT":
        return "limit"
    return "unknown"


@dataclass
class _Leg:
    """One fill, or one pro-rata part of a sign-flipping fill."""

    fill_id: Any
    order_id: str
    time_ms: int
    qty: Decimal
    price: Decimal
    realized: Decimal
    commission: Decimal
    commission_asset: str
    opening: bool
    split: bool = False


@dataclass
class _Episode:
    symbol: str
    position_side: str
    direction: str  # LONG / SHORT exposure of this episode
    legs: list[_Leg] = field(default_factory=list)
    started_before_window: bool = False
    closed: bool = False


def _legs_for(fill: dict[str, Any], parts: list[tuple[Decimal, bool]]) -> list[_Leg]:
    qty = _dec(fill.get("qty"))
    realized = _dec(fill.get("realizedPnl"))
    commission = _dec(fill.get("commission"))
    split = len(parts) > 1
    legs = []
    for part_qty, opening in parts:
        share = part_qty / qty if qty else ZERO
        legs.append(
            _Leg(
                fill_id=fill.get("id"),
                order_id=str(fill.get("orderId")),
                time_ms=_ms(fill.get("time")),
                qty=part_qty,
                price=_dec(fill.get("price")),
                # Binance books a flip fill's realized P&L on the closed part only.
                realized=ZERO if opening else realized,
                commission=commission * share,
                commission_asset=str(fill.get("commissionAsset") or "USDT"),
                opening=opening,
                split=split,
            )
        )
    return legs


def build_episodes(fills: list[dict[str, Any]]) -> list[_Episode]:
    """Walk the fills into flat-to-flat episodes per (symbol, positionSide)."""
    ordered = sorted(fills, key=lambda f: (_ms(f.get("time")), f.get("id") or 0))
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fill in ordered:
        side = str(fill.get("positionSide") or "BOTH").upper()
        by_key[(str(fill.get("symbol")), side)].append(fill)

    episodes: list[_Episode] = []
    for (symbol, position_side), key_fills in sorted(by_key.items()):
        if position_side in ("LONG", "SHORT"):
            episodes += _walk_hedge(symbol, position_side, key_fills)
        else:
            episodes += _walk_one_way(symbol, key_fills)
    return episodes


def _walk_hedge(symbol: str, side: str, fills: list[dict[str, Any]]) -> list[_Episode]:
    opening_side = "BUY" if side == "LONG" else "SELL"
    episodes: list[_Episode] = []
    current: _Episode | None = None
    position = ZERO
    for fill in fills:
        qty = _dec(fill.get("qty"))
        opening = str(fill.get("side")).upper() == opening_side
        if current is None:
            current = _Episode(symbol, side, side, started_before_window=not opening)
        current.legs += _legs_for(fill, [(qty, opening)])
        position += qty if opening else -qty
        if position <= 0 and not current.started_before_window:
            current.closed = True
            episodes.append(current)
            current, position = None, ZERO
        elif current.started_before_window and opening:
            # The pre-window remainder ended and a fresh position starts here:
            # close the incomplete episode and restart from this opening fill.
            current.legs.pop()
            episodes.append(current)
            current = _Episode(symbol, side, side)
            current.legs += _legs_for(fill, [(qty, True)])
            position = qty
    if current is not None:
        episodes.append(current)
    return episodes


def _walk_one_way(symbol: str, fills: list[dict[str, Any]]) -> list[_Episode]:
    episodes: list[_Episode] = []
    current: _Episode | None = None
    net = ZERO
    for fill in fills:
        qty = _dec(fill.get("qty"))
        signed = qty if str(fill.get("side")).upper() == "BUY" else -qty
        if current is None:
            # Without a known starting position, a fill that carries realized P&L
            # is closing a position opened before the window.
            pre_window = _dec(fill.get("realizedPnl")) != 0
            direction = (
                ("SHORT" if signed > 0 else "LONG")
                if pre_window
                else ("LONG" if signed > 0 else "SHORT")
            )
            current = _Episode(
                symbol, "BOTH", direction, started_before_window=pre_window
            )
            if pre_window:
                current.legs += _legs_for(fill, [(qty, False)])
                episodes.append(current)
                current, net = None, ZERO
                continue
        new_net = net + signed
        if net != 0 and (new_net == 0 or (net > 0) != (new_net > 0)):
            closing_qty = abs(net)
            opening_qty = qty - closing_qty
            parts = [(closing_qty, False)] + (
                [(opening_qty, True)] if opening_qty > 0 else []
            )
            legs = _legs_for(fill, parts)
            current.legs.append(legs[0])
            current.closed = True
            episodes.append(current)
            current = None
            if len(legs) > 1:
                current = _Episode(symbol, "BOTH", "LONG" if new_net > 0 else "SHORT")
                current.legs.append(legs[1])
        else:
            opening = abs(new_net) > abs(net)
            current.legs += _legs_for(fill, [(qty, opening)])
        net = new_net
        if net == 0:
            current = None
    if current is not None:
        episodes.append(current)
    return episodes


def _episode_record(
    index: int,
    episode: _Episode,
    orders_by_id: dict[str, dict[str, Any]],
    order_strategy: dict[str, str],
) -> dict[str, Any]:
    opening = [leg for leg in episode.legs if leg.opening]
    closing = [leg for leg in episode.legs if not leg.opening]
    realized = sum((leg.realized for leg in closing), ZERO)
    assets = {leg.commission_asset for leg in episode.legs if leg.commission}
    commission = sum((leg.commission for leg in episode.legs), ZERO)
    net = realized - commission if assets <= {"USDT"} else None

    shares: dict[str, Decimal] = defaultdict(lambda: ZERO)
    unmapped_qty = ZERO
    for leg in opening:
        strategy = order_strategy.get(leg.order_id)
        if strategy:
            shares[strategy] += leg.qty
        else:
            unmapped_qty += leg.qty
    open_qty = sum((leg.qty for leg in opening), ZERO)

    if episode.started_before_window:
        status, strategy_id = "started_before_window", None
    elif not episode.closed:
        status, strategy_id = "open_at_window_end", None
    elif unmapped_qty > 0:
        status, strategy_id = "unmapped", None
    elif len(shares) == 1:
        status, strategy_id = "attributed", next(iter(shares))
    else:
        status, strategy_id = "multi_strategy", None

    close_kinds: dict[str, int] = defaultdict(int)
    for leg in closing:
        close_kinds[close_kind(orders_by_id.get(leg.order_id))] += 1

    return {
        "episode": index,
        "symbol": episode.symbol,
        "position_side": episode.position_side,
        "direction": episode.direction,
        "status": status,
        "strategy_id": strategy_id,
        "strategy_shares": (
            {s: float(q / open_qty) for s, q in sorted(shares.items())}
            if open_qty
            else {}
        ),
        "unmapped_opening_qty": float(unmapped_qty),
        "open_time_ms": min(leg.time_ms for leg in episode.legs),
        "close_time_ms": max(leg.time_ms for leg in episode.legs)
        if episode.closed
        else None,
        "opening_qty": float(open_qty),
        "realized_pnl": float(realized),
        "commission": float(commission),
        "commission_assets": sorted(assets),
        "net_pnl": None if net is None else float(net),
        "close_kinds": dict(sorted(close_kinds.items())),
        "split_fill": any(leg.split for leg in episode.legs),
        "opening_order_ids": sorted({leg.order_id for leg in opening}),
        "closing_order_ids": sorted({leg.order_id for leg in closing}),
        "fill_ids": [leg.fill_id for leg in episode.legs],
    }


def build_ground_truth_v2(
    fills: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    order_strategy: dict[str, str],
    position_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The per-strategy report from exchange episodes, with ledger rows as annotations."""
    orders_by_id = {str(o.get("orderId")): o for o in orders}
    episodes = [
        _episode_record(i, e, orders_by_id, order_strategy)
        for i, e in enumerate(build_episodes(fills))
    ]

    rows_by_entry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in position_rows or []:
        if row.get("entry_order_id"):
            rows_by_entry[str(row["entry_order_id"])].append(row)
    episode_by_order: dict[str, int] = {}
    for ep in episodes:
        for order_id in ep["opening_order_ids"]:
            episode_by_order[order_id] = ep["episode"]
    for ep in episodes:
        ep["ledger_rows"] = [
            {
                "strategy_position_id": row.get("strategy_position_id"),
                "strategy_id": row.get("strategy_id"),
                "status": row.get("status"),
                "close_reason": row.get("close_reason"),
            }
            for order_id in ep["opening_order_ids"]
            for row in rows_by_entry.get(order_id, [])
        ]

    strategies = _per_strategy(episodes)
    status_counts: dict[str, int] = defaultdict(int)
    for ep in episodes:
        status_counts[ep["status"]] += 1
    total_realized = sum((_dec(f.get("realizedPnl")) for f in fills), ZERO)
    fill_assets = {
        str(f.get("commissionAsset") or "USDT")
        for f in fills
        if _dec(f.get("commission"))
    }
    total_commission = sum((_dec(f.get("commission")) for f in fills), ZERO)
    episode_net = sum(
        (_dec(ep["net_pnl"]) for ep in episodes if ep["net_pnl"] is not None), ZERO
    )
    raw_net = total_realized - total_commission if fill_assets <= {"USDT"} else None

    return {
        # First, so no reader misses how much of the window is not attributed.
        "unmatched_headline": {
            "episodes": len(episodes),
            "attributed": status_counts.get("attributed", 0),
            "not_attributed": len(episodes) - status_counts.get("attributed", 0),
            "by_status": dict(sorted(status_counts.items())),
            "fills": len(fills),
            "unmapped_opening_orders": sum(
                1
                for ep in episodes
                for order_id in ep["opening_order_ids"]
                if order_id not in order_strategy
            ),
        },
        "reconciliation": {
            "sum_episode_net_pnl": float(episode_net),
            "sum_fill_realized_pnl_minus_commission": None
            if raw_net is None
            else float(raw_net),
            "gap": None if raw_net is None else float(raw_net - episode_net),
            "note": "the gap is the P&L of episodes whose net is null (non-USDT fee)",
        },
        "strategies": strategies,
        "external_closes": _ledger_external_closes(episodes),
        "episodes": episodes,
    }


def _per_strategy(episodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    attributed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    involved: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ep in episodes:
        if ep["status"] == "attributed":
            attributed[ep["strategy_id"]].append(ep)
        else:
            for strategy in ep["strategy_shares"]:
                involved[strategy][ep["status"]] += 1
    for strategy in sorted(set(attributed) | set(involved)):
        items = sorted(
            attributed.get(strategy, []), key=lambda e: e["close_time_ms"] or 0
        )
        nets = [e["net_pnl"] for e in items]
        known = [n for n in nets if n is not None]
        streak = worst = 0
        for net in known:
            streak = streak + 1 if net < 0 else 0
            worst = max(worst, streak)
        kinds: dict[str, int] = defaultdict(int)
        for e in items:
            for kind, n in e["close_kinds"].items():
                kinds[kind] += n
        out[strategy] = {
            "episodes": len(items),
            "wins": sum(1 for n in known if n > 0),
            "losses": sum(1 for n in known if n < 0),
            "flat": sum(1 for n in known if n == 0),
            "net_unknown": len(nets) - len(known),
            "max_consecutive_losses": worst,
            "realized_pnl": sum(e["realized_pnl"] for e in items),
            "commission": sum(e["commission"] for e in items),
            "net_pnl": sum(known),
            "close_kinds": dict(sorted(kinds.items())),
            "not_attributed_episodes": dict(sorted(involved.get(strategy, {}).items())),
        }
    return out


def _ledger_external_closes(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Where the ledger's closed_externally rows actually closed on the exchange."""
    per_strategy: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ep in episodes:
        for row in ep["ledger_rows"]:
            if row.get("status") != "closed_externally":
                continue
            kinds = ep["close_kinds"] or {"open_at_window_end": 1}
            for kind in kinds:
                per_strategy[str(row.get("strategy_id"))][kind] += 1
    return {s: dict(sorted(k.items())) for s, k in sorted(per_strategy.items())}
