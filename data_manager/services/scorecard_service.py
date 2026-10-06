"""Cost-aware, read-only strategy scorecard calculations."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
MARK_HOURS = (0, 8, 16)


def money(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def timestamp(row: dict[str, Any]) -> datetime | None:
    value = row.get("fill_time") or row.get("close_time") or row.get("timestamp")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _intervals(values: list[Decimal], wins: int) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"n": 0, "low": None, "high": None}
    p = Decimal(wins) / Decimal(n)
    z = Decimal("1.959963984540054")
    denominator = Decimal(1) + z * z / Decimal(n)
    centre = (p + z * z / (Decimal(2) * Decimal(n))) / denominator
    spread = z * ((p * (1 - p) / Decimal(n) + z * z / (Decimal(4) * Decimal(n) ** 2)).sqrt()) / denominator
    return {"n": n, "low": decimal_string(max(ZERO, centre - spread)), "high": decimal_string(min(Decimal(1), centre + spread))}


def _bootstrap(values: list[Decimal]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "low": None, "high": None}
    rng = random.Random(468)
    means = [sum(rng.choice(values) for _ in values) / Decimal(len(values)) for _ in range(1000)]
    means.sort()
    return {
        "n": len(values),
        "low": decimal_string(means[25]),
        "high": decimal_string(means[974]),
    }


def _metric(
    values: list[Decimal],
    fees: Decimal,
    funding: Decimal,
    minimum: int,
    net_values: list[Decimal] | None = None,
) -> dict[str, Any]:
    n = len(values)
    gross = sum(values, ZERO)
    net = gross - fees - funding
    wins = [value for value in values if value > ZERO]
    losses = [value for value in values if value < ZERO]
    payoff = (sum(wins, ZERO) / abs(sum(losses, ZERO))) if losses else None
    expectancy = net / Decimal(n) if n else None
    ex_top1 = None
    if n > 1:
        trimmed = values.copy()
        trimmed.remove(max(trimmed))
        ex_top1 = (sum(trimmed, ZERO) - fees - funding) / Decimal(len(trimmed))
    running = ZERO
    peak = ZERO
    drawdown = ZERO
    drawdown_values = net_values or values
    funding_per_trade = funding / Decimal(n) if n else ZERO
    for value in drawdown_values:
        value -= funding_per_trade
        running += value
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    median = sorted(values)[n // 2] if n and n % 2 else ((sorted(values)[n // 2 - 1] + sorted(values)[n // 2]) / 2 if n else None)
    return {
        "n_trades": n,
        "gross_pnl": decimal_string(gross),
        "fees": decimal_string(fees),
        "funding_allocated": decimal_string(funding),
        "net_pnl": decimal_string(net),
        "win_rate": decimal_string(Decimal(len(wins)) / Decimal(n)) if n else None,
        "avg_win": decimal_string(sum(wins, ZERO) / Decimal(len(wins))) if wins else None,
        "avg_loss": decimal_string(sum(losses, ZERO) / Decimal(len(losses))) if losses else None,
        "payoff": decimal_string(payoff),
        "expectancy_per_trade": decimal_string(expectancy),
        "expectancy_ex_top1": decimal_string(ex_top1),
        "median_trade": decimal_string(median),
        "max_drawdown": decimal_string(drawdown),
        "fee_share_of_gross": decimal_string(fees / abs(gross)) if gross else None,
        "sample_ok": n >= minimum,
        "win_rate_interval": _intervals(values, len(wins)),
        "expectancy_interval": _bootstrap(values),
    }


def _group_key(close: dict[str, Any], decision: dict[str, Any], group_by: str) -> str:
    strategy = close.get("strategy_id") or decision.get("strategy_id")
    symbol = close.get("symbol") or decision.get("symbol") or "unknown"
    mode = close.get("cio_mode") or decision.get("source") or decision.get("mode") or "unknown"
    if group_by == "strategy_symbol":
        return f"{strategy or 'unattributed'}:{symbol}"
    if group_by == "cio_mode":
        return str(mode)
    return str(strategy or "unattributed")


def calculate_scorecard(
    execution_events: Iterable[dict[str, Any]],
    pnl_events: Iterable[dict[str, Any]] = (),
    cio_decisions: Iterable[dict[str, Any]] = (),
    funding_events: Iterable[dict[str, Any]] = (),
    positions: Iterable[dict[str, Any]] = (),
    *,
    group_by: str = "strategy",
    minimum_trades: int = 30,
) -> dict[str, Any]:
    """Calculate a scorecard from already-read documents without writing data."""
    decisions = {str(row.get("decision_id")): row for row in cio_decisions if row.get("decision_id")}
    closes: list[dict[str, Any]] = []
    entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in execution_events:
        kind = event.get("event_type")
        position_id = str(event.get("position_id") or event.get("payload", {}).get("position_id") or event.get("order_id") or "")
        if kind in {"filled", "partial_fill"}:
            if event.get("pnl") is not None or event.get("realized_pnl") is not None or event.get("close_reason"):
                closes.append({**event, "position_id": position_id})
            else:
                entries[position_id].append(event)
    close_keys = {str(row.get("position_id") or row.get("order_id") or "") for row in closes}
    for event in pnl_events:
        if event.get("pnl_kind") == "closed" and event.get("realized_pnl_usd") is not None:
            position_id = str(event.get("position_id") or event.get("order_id") or "")
            if position_id not in close_keys:
                closes.append({**event, "pnl": event.get("realized_pnl_usd"), "position_id": position_id})
                close_keys.add(position_id)
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"values": [], "net_values": [], "fees": ZERO, "funding": ZERO, "open_positions": [], "unrealized": ZERO})
    total_fees = ZERO
    total_funding = ZERO
    for close in sorted(closes, key=lambda row: timestamp(row) or datetime.min.replace(tzinfo=UTC)):
        decision = decisions.get(str(close.get("decision_id")), {})
        key = _group_key(close, decision, group_by)
        group = groups[key]
        value = money(close.get("pnl") if close.get("pnl") is not None else close.get("realized_pnl_usd"))
        fee = money(close.get("fee"))
        for entry in entries.get(str(close.get("position_id")), []):
            fee += money(entry.get("fee"))
        group["values"].append(value)
        group["fees"] += fee
        group["net_values"].append(value - fee)
        total_fees += fee
    for position in positions:
        if str(position.get("status", "")).lower() in {"closed", "complete"}:
            continue
        decision = decisions.get(str(position.get("decision_id")), {})
        key = _group_key(position, decision, group_by)
        group = groups[key]
        unrealized = money(position.get("unrealized_pnl_usd"))
        group["unrealized"] += unrealized
        group["open_positions"].append({"position_id": position.get("position_id"), "unrealized_pnl": decimal_string(unrealized)})
    position_groups: dict[str, str] = {}
    for close in closes:
        decision = decisions.get(str(close.get("decision_id")), {})
        position_groups[str(close.get("position_id"))] = _group_key(close, decision, group_by)
    for position in positions:
        decision = decisions.get(str(position.get("decision_id")), {})
        position_groups.setdefault(str(position.get("position_id")), _group_key(position, decision, group_by))
    for funding in funding_events:
        amount = money(funding.get("funding_fee") if funding.get("funding_fee") is not None else funding.get("amount"))
        total_funding += amount
        funding_time = timestamp(funding)
        position_id = str(funding.get("position_id") or "")
        if funding_time and funding_time.hour in MARK_HOURS and position_id in position_groups:
            groups[position_groups[position_id]]["funding"] += amount
    result_groups = []
    for key, group in sorted(groups.items()):
        metrics = _metric(group["values"], group["fees"], group["funding"], minimum_trades, group["net_values"])
        metrics.update({"group": key, "open_positions": group["open_positions"], "unrealized_pnl": decimal_string(group["unrealized"])})
        result_groups.append(metrics)
    total_gross = sum((money(item["gross_pnl"]) for item in result_groups), ZERO)
    total_net = sum((money(item["net_pnl"]) for item in result_groups), ZERO)
    return {
        "groups": result_groups,
        "unattributed": next((item for item in result_groups if item["group"] == "unattributed"), None),
        "total_net": decimal_string(total_net),
        "total_gross": decimal_string(total_gross),
        "total_fees": decimal_string(total_fees),
        "total_funding": decimal_string(total_funding),
        "funding_unallocated": decimal_string(total_funding - sum((money(item["funding_allocated"]) for item in result_groups), ZERO)),
        "open_entry_fee_delta": "0",
        "group_by": group_by,
    }


class ScorecardService:
    """Read the audit collections and delegate to the pure calculator."""

    def __init__(self, mongodb: Any) -> None:
        self.mongodb = mongodb

    async def calculate(self, **kwargs: Any) -> dict[str, Any]:
        collections = await self._read_collections(kwargs.get("start"), kwargs.get("end"))
        return calculate_scorecard(**collections, **{key: value for key, value in kwargs.items() if key not in {"start", "end"}})

    async def _read_collections(self, start: datetime | None, end: datetime | None) -> dict[str, list[dict[str, Any]]]:
        return {
            "execution_events": await self.mongodb.find_filtered("execution_events", start=start, end=end, limit=10000, sort_order=1),
            "pnl_events": await self.mongodb.find_filtered("pnl_events", start=start, end=end, limit=10000, sort_order=1),
            "cio_decisions": await self.mongodb.find_filtered("cio_decisions", start=start, end=end, limit=10000, sort_order=1),
            "funding_events": await self.mongodb.find_filtered("funding_rates", start=start, end=end, limit=10000, sort_order=1),
            "positions": await self.mongodb.find_filtered("positions", limit=10000, sort_order=1),
        }
