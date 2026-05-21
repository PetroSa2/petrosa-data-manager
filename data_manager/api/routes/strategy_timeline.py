"""Strategy lifecycle timeline endpoint (P3.3, petrosa_k8s#600)."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Path, Query

import data_manager.api.app as api_module
from data_manager.db.repositories.strategy_timeline_repository import (
    ALL_TYPES,
    StrategyTimelineRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _repo() -> StrategyTimelineRepository | None:
    if not api_module.db_manager:
        return None
    if not getattr(api_module.db_manager, "mongodb_adapter", None):
        return None
    return StrategyTimelineRepository(
        mysql_adapter=api_module.db_manager.mysql_adapter,
        mongodb_adapter=api_module.db_manager.mongodb_adapter,
    )


@router.get("/strategies/{strategy_id}/timeline")
async def get_strategy_timeline(
    strategy_id: str = Path(..., description="Strategy identifier"),
    from_ts: datetime | None = Query(
        None,
        alias="from",
        description="Start of the timeline window (UTC). Inclusive.",
    ),
    to_ts: datetime | None = Query(
        None,
        alias="to",
        description="End of the timeline window (UTC). Exclusive.",
    ),
    types: str | None = Query(
        None,
        description=(
            "Comma-separated list of event types to include. Defaults to "
            f"all of: {sorted(ALL_TYPES)}"
        ),
    ),
    limit: int = Query(
        200,
        ge=1,
        le=1000,
        description="Page size (1..1000, default 200)",
    ),
    cursor: str | None = Query(
        None,
        description=(
            "Opaque cursor returned as `next_cursor` from a previous page. "
            "Pass back verbatim to fetch the next page."
        ),
    ),
) -> dict:
    """Return a chronologically-merged timeline of events for one strategy.

    Joins five sources (config audit, characterizations, lifecycle events,
    intents, decisions, executions) into a single ``events`` list ordered
    by ``(ts, event_id)``. Cursor pagination keeps order stable across
    sources that share a millisecond.

    Returns 503 when the database is unavailable.
    """
    repo = _repo()
    if repo is None:
        raise HTTPException(status_code=503, detail="Database not available")

    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None

    try:
        return await repo.get_timeline(
            strategy_id=strategy_id,
            from_ts=from_ts,
            to_ts=to_ts,
            types=type_list,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        logger.error(
            "timeline: query failed for %s: %s",
            strategy_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="timeline query failed") from exc
