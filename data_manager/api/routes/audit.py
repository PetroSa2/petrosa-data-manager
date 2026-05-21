"""Audit-trail query endpoints (#605 P4.5).

Exposes the cross-service decision audit-trail with composable filters
on ``strategy_id``, CIO ``action`` category, ``decision_id``, ``symbol``,
and time window. The underlying storage is the MongoDB
``cio_decisions`` collection populated by ``DecisionConsumer``; indexes
on each filter field exist already so any single filter or combination
is index-served.

See also: ``data_manager/db/repositories/audit_repository.py:query_decisions``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/audit/decisions")
async def list_decision_audit_trail(
    strategy_id: str | None = Query(None, description="Filter by strategy_id"),
    action: str | None = Query(
        None,
        description=(
            "Filter by CIO action verb (e.g. execute, admit, veto, "
            "down_weight, fail_safe, skip). Lower-cased per the "
            "petrosa-cio producer convention."
        ),
    ),
    decision_id: str | None = Query(
        None,
        description=(
            "Filter by a specific CIO decision_id. Cross-service "
            "join key — matches execution_events.decision_id and "
            "pnl_events.decision_id."
        ),
    ),
    symbol: str | None = Query(None, description="Filter by trading pair symbol"),
    from_time: datetime | None = Query(
        None,
        alias="from",
        description="Inclusive start of the timestamp window (ISO 8601)",
    ),
    to_time: datetime | None = Query(
        None,
        alias="to",
        description="Exclusive end of the timestamp window (ISO 8601)",
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of decisions returned (default 100, max 1000)",
    ),
) -> dict:
    """List CIO decision audit-trail rows filtered by any combination of
    ``strategy_id`` / ``action`` / ``decision_id`` / ``symbol`` and an
    optional time window. Results are newest-first.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories import AuditRepository

        audit_repo = AuditRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        decisions = await audit_repo.query_decisions(
            strategy_id=strategy_id,
            action=action,
            decision_id=decision_id,
            symbol=symbol,
            start=from_time,
            end=to_time,
            limit=limit,
        )

        return {
            "count": len(decisions),
            "filters": {
                "strategy_id": strategy_id,
                "action": action,
                "decision_id": decision_id,
                "symbol": symbol,
                "from": from_time.isoformat() if from_time else None,
                "to": to_time.isoformat() if to_time else None,
            },
            "limit": limit,
            "decisions": decisions,
        }

    except Exception as e:
        logger.error(
            "audit_decisions_endpoint_failed",
            extra={"error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to query decision audit-trail"
        ) from e
