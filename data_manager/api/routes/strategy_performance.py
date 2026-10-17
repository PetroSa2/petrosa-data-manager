"""Per-strategy realized performance report."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.models.execution_event import KNOWN_EVENT_TYPES
from data_manager.services.strategy_performance import build_strategy_performance

router = APIRouter()


@router.get("/strategy/performance")
async def get_strategy_performance(
    from_: datetime = Query(
        ..., alias="from", description="Inclusive UTC window start"
    ),
    to: datetime = Query(..., description="Exclusive UTC window end"),
) -> dict[str, Any]:
    if from_ >= to:
        raise HTTPException(status_code=422, detail="from must be before to")
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    mongodb = getattr(api_module.db_manager, "mongodb_adapter", None)
    if mongodb is None:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    query = {
        "event_type": {"$in": sorted(KNOWN_EVENT_TYPES)},
        "timestamp": {"$gte": from_, "$lt": to},
    }
    try:
        cursor = mongodb.db["execution_events"].find(query).sort("timestamp", 1)
        rows = await cursor.to_list(length=50_000)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="strategy performance read failed"
        ) from exc

    return {
        "strategies": build_strategy_performance(rows, start=from_, end=to),
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "execution_events",
    }
