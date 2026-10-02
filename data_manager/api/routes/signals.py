"""Typed durable signal upsert, replay, and kline coverage endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from prometheus_client import Counter

import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.repositories.signal_repository import SignalRepository
from data_manager.models.signal import SignalRecord

router = APIRouter(prefix="/api/v1/signals")
signal_revision_conflicts_total = Counter(
    "signal_revision_conflicts_total",
    "Signal writes rejected because a non-null revision value differed",
)


def _repository() -> SignalRepository:
    manager = api_module.db_manager
    adapter = getattr(manager, "mysql_adapter", None) if manager else None
    if adapter is None:
        raise HTTPException(status_code=503, detail="MySQL not available")
    return SignalRepository(adapter)


@router.post("")
async def upsert_signal(signal: SignalRecord) -> dict[str, Any]:
    return await asyncio.to_thread(_repository().upsert, signal)


@router.get("/replay")
async def replay_signals(
    strategy: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    from_ts: datetime | None = Query(None, alias="from"),
    to_ts: datetime | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=10_000),
    offset: int = Query(0, ge=0),
    include_legacy: bool = False,
) -> dict[str, Any]:
    if from_ts and to_ts and from_ts >= to_ts:
        raise HTTPException(status_code=400, detail="from must be earlier than to")
    try:
        rows, total = await asyncio.to_thread(
            _repository().replay,
            strategy=strategy, symbol=symbol, timeframe=timeframe,
            from_ts=from_ts, to_ts=to_ts, limit=limit, offset=offset,
            include_legacy=include_legacy,
        )
    except DatabaseError as exc:
        raise HTTPException(status_code=503, detail="signal replay unavailable") from exc
    return {
        "data": rows,
        "pagination": {
            "total": total, "limit": limit, "offset": offset,
            "has_next": offset + limit < total,
        },
    }


@router.get("/replay/coverage")
async def replay_coverage(
    strategy: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    from_ts: datetime | None = Query(None, alias="from"),
    to_ts: datetime | None = Query(None, alias="to"),
) -> dict[str, Any]:
    if from_ts and to_ts and from_ts >= to_ts:
        raise HTTPException(status_code=400, detail="from must be earlier than to")
    try:
        return await asyncio.to_thread(
            _repository().coverage,
            strategy=strategy, symbol=symbol, timeframe=timeframe,
            from_ts=from_ts, to_ts=to_ts,
        )
    except DatabaseError as exc:
        raise HTTPException(status_code=503, detail="signal coverage unavailable") from exc
