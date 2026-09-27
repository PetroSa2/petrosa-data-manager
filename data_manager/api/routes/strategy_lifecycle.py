"""Typed API for operational strategy lifecycle state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

import data_manager.api.app as api_module
from data_manager.db.repositories.strategy_lifecycle_repository import (
    StrategyLifecycleRepository,
    event_id,
    utc_datetime,
)

router = APIRouter()
STRATEGY_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
STATE_PATTERN = r"^[a-z_]{1,32}$"


class LifecycleEventRequest(BaseModel):
    from_state: str | None = None
    to_state: str = Field(pattern=STATE_PATTERN)
    transitioned_by: str
    reason: str | None = None
    service: str | None = None
    metadata: dict[str, Any] | None = None
    transitioned_at: datetime | None = None


def _repo() -> StrategyLifecycleRepository:
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="Database not available")
    return StrategyLifecycleRepository(
        mysql_adapter=api_module.db_manager.mysql_adapter,
        mongodb_adapter=api_module.db_manager.mongodb_adapter,
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return utc_datetime(value).isoformat()


@router.post("/strategies/{strategy_id}/lifecycle/events", status_code=201)
async def create_event(
    payload: LifecycleEventRequest,
    strategy_id: str = Path(..., pattern=STRATEGY_ID_PATTERN),
) -> dict[str, Any]:
    try:
        document = payload.model_dump(exclude_none=True)
        document["strategy_id"] = strategy_id
        document["transitioned_at"] = utc_datetime(payload.transitioned_at)
        saved = await _repo().insert_event(document)
        return {
            "event_id": saved["event_id"],
            "strategy_id": strategy_id,
            "to_state": saved["to_state"],
            "transitioned_at": _iso(saved["transitioned_at"]),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="lifecycle persistence failed"
        ) from exc


@router.get("/strategies/{strategy_id}/lifecycle/state")
async def get_state(
    strategy_id: str = Path(..., pattern=STRATEGY_ID_PATTERN),
) -> dict[str, Any]:
    try:
        document = await _repo().get_state(strategy_id)
        return {
            "strategy_id": strategy_id,
            "state": document.get("to_state") if document else None,
            "transitioned_at": _iso(document.get("transitioned_at"))
            if document
            else None,
            "event_id": event_id(document) if document else None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="lifecycle query failed") from exc


@router.get("/strategies/{strategy_id}/lifecycle/events")
async def get_events(
    strategy_id: str = Path(..., pattern=STRATEGY_ID_PATTERN),
    limit: int = Query(500, ge=1, le=1000),
    order: str = Query("asc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    try:
        documents = await _repo().get_events(strategy_id, limit, order)
        events = [
            {
                **{key: value for key, value in document.items() if key != "_id"},
                "event_id": event_id(document),
                "transitioned_at": _iso(document.get("transitioned_at")),
            }
            for document in documents
        ]
        return {"strategy_id": strategy_id, "events": events, "count": len(events)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="lifecycle query failed") from exc
