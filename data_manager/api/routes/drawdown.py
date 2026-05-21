"""Portfolio drawdown query endpoint (P4.2, #602).

Exposes :class:`DrawdownService.compute` over HTTP so operators can poll
the current drawdown / envelope state on demand, alongside the
periodic NATS breach surface. The endpoint deliberately mirrors the
shape of ``DrawdownResult.to_dict()`` — dashboards consume the same
payload from both the HTTP route and the
``portfolio.drawdown.breach.>`` NATS subject.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/portfolio/drawdown")
async def get_strategy_drawdown(
    strategy_id: str = Query(..., description="Strategy identifier to evaluate"),
    from_time: datetime | None = Query(
        None,
        alias="from",
        description=(
            "Inclusive lower bound of the PnL evaluation window (ISO 8601). "
            "Omit to use the full available history."
        ),
    ),
    to_time: datetime | None = Query(
        None,
        alias="to",
        description="Exclusive upper bound of the PnL evaluation window (ISO 8601)",
    ),
) -> dict:
    """Compute current drawdown for ``strategy_id`` vs its characterization envelope."""
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories.characterization_repository import (
            CharacterizationRepository,
        )
        from data_manager.portfolio.drawdown_service import DrawdownService

        char_repo = CharacterizationRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )
        service = DrawdownService(
            mongodb_adapter=api_module.db_manager.mongodb_adapter,
            characterization_repository=char_repo,
        )
        result = await service.compute(
            strategy_id=strategy_id,
            start=from_time,
            end=to_time,
        )
        return result.to_dict()
    except Exception as e:
        logger.error(
            "drawdown_endpoint_failed",
            extra={"strategy_id": strategy_id, "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to compute drawdown") from e
