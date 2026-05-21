"""Lifecycle reconstruction endpoints (#603 P4.3).

Two surfaces:

  * ``GET /api/v1/lifecycle/{decision_id}`` — full lifecycle for one
    decision (intents → decision → executions → pnl_events). Returns 404
    when the anchor decision doesn't exist.
  * ``GET /api/v1/lifecycle?strategy_id=...[&from=...&to=...&limit=...]``
    — newest-first lifecycle list for a strategy. Powers the P5.1
    strategy lifecycle view; #604 ("why did portfolio do X at time T")
    will build on the single-decision endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/lifecycle/{decision_id}")
async def get_lifecycle_by_decision(decision_id: str) -> dict:
    """Reconstruct the full lifecycle for ``decision_id``.

    Joins ``intents``, ``cio_decisions``, ``execution_events``, and
    ``pnl_events`` by ``decision_id``. Returns 404 when no
    ``cio_decisions`` row exists for the given id (the decision is the
    anchor — without it the join has no root).
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories.lifecycle_repository import (
            LifecycleRepository,
        )

        repo = LifecycleRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )
        result = await repo.reconstruct(decision_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"No decision found for decision_id={decision_id}",
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "lifecycle_endpoint_failed",
            extra={"decision_id": decision_id, "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to reconstruct lifecycle"
        ) from e


@router.get("/lifecycle")
async def list_lifecycle_by_strategy(
    strategy_id: str = Query(..., description="Strategy identifier"),
    from_time: datetime | None = Query(
        None,
        alias="from",
        description="Inclusive start of the decision-timestamp window",
    ),
    to_time: datetime | None = Query(
        None, alias="to", description="Exclusive end of the decision-timestamp window"
    ),
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description=(
            "Max decisions reconstructed in one call (default 50, max 200). "
            "Each decision triggers four collection reads — cap is tighter "
            "than the audit-trail endpoint."
        ),
    ),
) -> dict:
    """List reconstructed lifecycles for a strategy, newest first.

    Each entry is the same shape as the single-decision endpoint. Set
    ``limit`` carefully — every decision triggers four MongoDB queries.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories.lifecycle_repository import (
            LifecycleRepository,
        )

        repo = LifecycleRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )
        lifecycles = await repo.reconstruct_by_strategy(
            strategy_id,
            start=from_time,
            end=to_time,
            limit=limit,
        )
        return {
            "strategy_id": strategy_id,
            "count": len(lifecycles),
            "limit": limit,
            "from": from_time.isoformat() if from_time else None,
            "to": to_time.isoformat() if to_time else None,
            "lifecycles": lifecycles,
        }
    except Exception as e:
        logger.error(
            "lifecycle_strategy_endpoint_failed",
            extra={"strategy_id": strategy_id, "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to list strategy lifecycles"
        ) from e
