"""Portfolio state-at-time-T query endpoint (#604 P4.4).

``GET /api/v1/portfolio/state-at?at=<ts>[&strategy_id=<id>]`` answers the
operator question "why did the portfolio do X at time T?" by combining
the cumulative equity reconstruction (P4.1 substrate) with a slice of
recent decisions / executions / pnl_events leading up to T.

Powers the dashboard time-slider (FR34). The companion endpoint
``GET /api/v1/lifecycle/{decision_id}`` (#603) drills down from any
recent_decisions entry into the full per-decision lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/portfolio/state-at")
async def get_portfolio_state_at(
    at: datetime = Query(
        ..., description="Point-in-time timestamp (ISO 8601, exclusive upper bound)"
    ),
    strategy_id: str | None = Query(
        None, description="Optional strategy filter — omit for portfolio-wide state"
    ),
) -> dict:
    """Compute portfolio state + recent event chain at the given timestamp."""
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.portfolio.state_service import PortfolioStateService

        service = PortfolioStateService(
            mongodb_adapter=api_module.db_manager.mongodb_adapter,
        )
        result = await service.state_at(at, strategy_id=strategy_id)
        return result.to_dict()
    except Exception as e:
        logger.error(
            "portfolio_state_endpoint_failed",
            extra={"at": at.isoformat(), "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to compute portfolio state-at-time"
        ) from e
