"""Strategy-fidelity query endpoint (P2.3, #594).

Operators poll ``GET /api/v1/strategy/{strategy_id}/fidelity`` to see the
current FidelityResult on demand, in the same shape the NATS verdict
publisher emits on ``evaluator.strategy.<strategy_id>.verdict``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/strategy/{strategy_id}/fidelity")
async def get_strategy_fidelity(
    strategy_id: str,
    from_time: datetime | None = Query(
        None, alias="from", description="Inclusive start of the PnL window (ISO 8601)"
    ),
    to_time: datetime | None = Query(
        None, alias="to", description="Exclusive end of the PnL window (ISO 8601)"
    ),
) -> dict:
    """Current strategy-fidelity verdict for one strategy."""
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from data_manager.db.repositories.characterization_repository import (
            CharacterizationRepository,
        )
        from data_manager.strategies.fidelity_service import FidelityService

        char_repo = CharacterizationRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )
        service = FidelityService(
            mongodb_adapter=api_module.db_manager.mongodb_adapter,
            characterization_repository=char_repo,
        )
        result = await service.evaluate(strategy_id, start=from_time, end=to_time)
        return result.to_dict()
    except Exception as e:
        logger.error(
            "fidelity_endpoint_failed",
            extra={"strategy_id": strategy_id, "error": str(e)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to compute strategy fidelity"
        ) from e
