"""Durable CIO auto-resume registry API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

import data_manager.api.app as api_module
from data_manager.db.repositories.cio_auto_resume_repository import (
    CioAutoResumeRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/cio/auto-resume")
STRATEGY_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
STATUS_PATTERN = r"^[a-z_]{1,32}$"


class CioPauseEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(pattern=STRATEGY_ID_PATTERN)
    service: str
    status: str = Field(pattern=STATUS_PATTERN)
    paused_at: float
    last_unavailable_at: float
    min_pause_seconds: int
    flap_count: int
    attempts: int
    next_attempt_at: float
    resumed_at: float | None = None
    gave_up_at: float | None = None
    gave_up_reason: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _repo() -> CioAutoResumeRepository:
    manager = api_module.db_manager
    if manager is None or getattr(manager, "mongodb_adapter", None) is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return CioAutoResumeRepository(manager.mysql_adapter, manager.mongodb_adapter)


def _json(entry: dict[str, Any]) -> dict[str, Any]:
    entry.pop("_id", None)
    return CioPauseEntry.model_validate(entry).model_dump(mode="json")


@router.get("/entries")
async def list_entries(
    status: str | None = Query(None),
) -> dict[str, Any]:
    try:
        statuses = status.split(",") if status else None
        entries = await _repo().list_entries(statuses)
        result = [_json(entry) for entry in entries]
        return {"entries": result, "count": len(result)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cio_auto_resume_list_failed")
        raise HTTPException(
            status_code=503, detail="CIO auto-resume database unavailable"
        ) from exc


@router.get("/entries/{strategy_id}")
async def get_entry(strategy_id: str = Path(..., pattern=STRATEGY_ID_PATTERN)):
    try:
        entry = await _repo().get_entry(strategy_id)
        if not entry:
            raise HTTPException(status_code=404, detail="auto-resume entry not found")
        return _json(entry)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cio_auto_resume_get_failed")
        raise HTTPException(
            status_code=503, detail="CIO auto-resume database unavailable"
        ) from exc


@router.put("/entries/{strategy_id}")
async def put_entry(
    body: CioPauseEntry,
    strategy_id: str = Path(..., pattern=STRATEGY_ID_PATTERN),
):
    if body.strategy_id != strategy_id:
        raise HTTPException(status_code=422, detail="strategy_id must match path")
    try:
        return _json(await _repo().upsert_entry(body.model_dump()))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cio_auto_resume_put_failed")
        raise HTTPException(
            status_code=503, detail="CIO auto-resume database unavailable"
        ) from exc


@router.delete("/entries/{strategy_id}")
async def delete_entry(
    strategy_id: str = Path(..., pattern=STRATEGY_ID_PATTERN),
    reason: str = Query(""),
):
    try:
        removed = await _repo().delete_entry(strategy_id)
        logger.info(
            "cio_auto_resume_removed strategy_id=%s reason=%s", strategy_id, reason
        )
        return {"removed": removed}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cio_auto_resume_delete_failed")
        raise HTTPException(
            status_code=503, detail="CIO auto-resume database unavailable"
        ) from exc
