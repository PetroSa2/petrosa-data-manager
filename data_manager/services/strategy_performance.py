"""Historical per-strategy realized performance from execution events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from data_manager.services.pnl_calculator import CLOSE_EVENT_TYPES, PnlCalculator


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _event_pnl(row: dict[str, Any]) -> float | None:
    return _number(
        row.get("pnl") if row.get("pnl") is not None else row.get("realized_pnl")
    )


def _event_net_r(row: dict[str, Any]) -> float:
    return (
        _number(
            row.get("net_r") if row.get("net_r") is not None else row.get("net_r_total")
        )
        or 0.0
    )


def build_strategy_performance(
    rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    strategy_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strategy_id = str(row.get("strategy_id") or "").strip()
        if strategy_id:
            grouped[strategy_id].append(row)
    for strategy_id in strategy_ids:
        normalized = str(strategy_id or "").strip()
        if normalized:
            grouped.setdefault(normalized, [])

    result: list[dict[str, Any]] = []
    for strategy_id, strategy_rows in sorted(grouped.items()):
        close_rows = [
            row for row in strategy_rows if row.get("event_type") in CLOSE_EVENT_TYPES
        ]
        realized = 0.0
        wins = 0
        losses = 0
        net_r_total = 0.0
        if close_rows:
            for row in close_rows:
                pnl = _event_pnl(row)
                if pnl is not None:
                    realized += pnl
                    wins += pnl > 0
                    losses += pnl < 0
                net_r_total += _event_net_r(row)
            trade_count = len(close_rows)
        else:
            calculator = PnlCalculator()
            trade_count = 0
            for row in sorted(
                strategy_rows,
                key=lambda item: item.get("timestamp")
                or datetime.min.replace(tzinfo=UTC),
            ):
                impact = calculator.apply_fill(row)
                if impact is not None and impact.realized_pnl != 0:
                    trade_count += 1
                    wins += impact.realized_pnl > 0
                    losses += impact.realized_pnl < 0
            realized = calculator.strategy_pnl(strategy_id).realized

        result.append(
            {
                "strategy_id": strategy_id,
                "realized_pnl_usd": realized,
                "win_rate": wins / trade_count if trade_count else 0.0,
                "trade_count": trade_count,
                "net_r_total": net_r_total,
                "from": start.isoformat(),
                "to": end.isoformat(),
            }
        )
    return result
