"""System trade audit-trail endpoints (#529).

`GET /api/v1/trades` — queryable fill history from `execution_events`
`GET /api/v1/trades/summary` — aggregated stats (PnL, win rate, fees)

These read the system execution audit trail produced by tradeengine → NATS
→ execution_events, distinct from market-data `/data/trades`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.services.pnl_calculator import PnlCalculator

logger = logging.getLogger(__name__)

router = APIRouter()

_FILL_TYPES = ("filled", "partial_fill")

# PnL replay needs the whole ordered fill stream, so the summary cannot paginate.
# Cap the scan instead of letting a wide window pull the collection into memory.
_SUMMARY_MAX_FILLS = 50_000


def _serialize_trade(row: dict[str, Any]) -> dict[str, Any]:
    fill_qty = row.get("fill_qty")
    if fill_qty is None:
        fill_qty = row.get("fill_quantity")
    fill_price = row.get("fill_price")
    if fill_price is None:
        fill_price = row.get("price")
    fill_time = row.get("fill_time") or row.get("timestamp")
    if isinstance(fill_time, datetime):
        fill_time = fill_time.isoformat()
    fee = row.get("fee")
    if fee is None:
        fee = row.get("fees")
    timestamp = row.get("timestamp")
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat()
    return {
        "decision_id": row.get("decision_id"),
        "strategy_id": row.get("strategy_id"),
        "order_id": row.get("order_id"),
        "event_type": row.get("event_type"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "fill_price": fill_price,
        "fill_quantity": fill_qty,
        "fill_qty": fill_qty,
        "fill_time": fill_time,
        "fee": fee,
        "fee_asset": row.get("fee_asset"),
        "pnl": row.get("pnl"),
        "timestamp": timestamp,
        "reason": row.get("reason"),
    }


def _build_fill_query(
    *,
    strategy_id: str | None,
    symbol: str | None,
    start_date: datetime | None,
    end_date: datetime | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"event_type": {"$in": list(_FILL_TYPES)}}
    if strategy_id:
        query["strategy_id"] = strategy_id
    if symbol:
        query["symbol"] = symbol
    if start_date is not None or end_date is not None:
        ts_range: dict[str, Any] = {}
        if start_date is not None:
            ts_range["$gte"] = start_date
        if end_date is not None:
            ts_range["$lt"] = end_date
        query["timestamp"] = ts_range
    return query


@router.get("/trades")
async def get_system_trades(
    strategy_id: str | None = Query(None, description="Filter by strategy_id"),
    symbol: str | None = Query(None, description="Filter by trading pair"),
    start_date: datetime | None = Query(
        None, description="Inclusive start of the trade window (UTC)"
    ),
    end_date: datetime | None = Query(
        None, description="Exclusive end of the trade window (UTC)"
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max trades to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> dict[str, Any]:
    """Return system trade fills from the `execution_events` audit trail (#529)."""
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    mongodb = getattr(api_module.db_manager, "mongodb_adapter", None)
    if mongodb is None:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    query = _build_fill_query(
        strategy_id=strategy_id,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    collection = mongodb.db["execution_events"]
    try:
        total = await collection.count_documents(query)
        cursor = collection.find(query).sort("timestamp", -1).skip(offset).limit(limit)
        rows = await cursor.to_list(length=limit)
    except Exception as exc:
        logger.error("system trades: read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="trades read failed") from exc

    return {
        "trades": [_serialize_trade(r) for r in rows],
        "pagination": {
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_next": offset + len(rows) < total,
            "has_previous": offset > 0,
        },
        "filters": {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "source": "execution_events",
    }


@router.get("/trades/summary")
async def get_system_trades_summary(
    strategy_id: str | None = Query(None, description="Filter by strategy_id"),
    symbol: str | None = Query(None, description="Filter by trading pair"),
    start_date: datetime | None = Query(
        None, description="Inclusive start of the trade window (UTC)"
    ),
    end_date: datetime | None = Query(
        None, description="Exclusive end of the trade window (UTC)"
    ),
) -> dict[str, Any]:
    """Return aggregated trade stats from `execution_events` fills (#529)."""
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    mongodb = getattr(api_module.db_manager, "mongodb_adapter", None)
    if mongodb is None:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    query = _build_fill_query(
        strategy_id=strategy_id,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    try:
        cursor = (
            mongodb.db["execution_events"]
            .find(query)
            .sort("timestamp", 1)
            .limit(_SUMMARY_MAX_FILLS)
        )
        rows = await cursor.to_list(length=_SUMMARY_MAX_FILLS)
    except Exception as exc:
        logger.error("system trades summary: read failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail="trades summary read failed"
        ) from exc

    calc = PnlCalculator()
    wins = 0
    losses = 0
    fee_total = 0.0
    for row in rows:
        fee = row.get("fee")
        if fee is None:
            fee = row.get("fees")
        try:
            if fee is not None:
                fee_total += float(fee)
        except (TypeError, ValueError):
            pass
        impact = calc.apply_fill(row)
        if impact is not None and impact.realized_pnl != 0:
            if impact.realized_pnl > 0:
                wins += 1
            else:
                losses += 1

    closed = wins + losses
    win_rate = (wins / closed) if closed else 0.0
    if strategy_id:
        breakdown = calc.strategy_pnl(strategy_id)
    else:
        breakdown = calc.portfolio_pnl()

    publisher_pnl = 0.0
    publisher_pnl_count = 0
    for row in rows:
        raw_pnl = row.get("pnl")
        if raw_pnl is None:
            continue
        try:
            publisher_pnl += float(raw_pnl)
            publisher_pnl_count += 1
        except (TypeError, ValueError):
            continue

    return {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "fills": len(rows),
        "closed_rounds": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": breakdown.realized,
        "unrealized_pnl": breakdown.unrealized,
        "fee_total": fee_total,
        "publisher_pnl_sum": publisher_pnl if publisher_pnl_count else None,
        "publisher_pnl_count": publisher_pnl_count,
        "truncated": len(rows) >= _SUMMARY_MAX_FILLS,
        "filters": {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "source": "execution_events",
    }
