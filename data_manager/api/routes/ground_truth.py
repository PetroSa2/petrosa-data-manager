"""Per-strategy ground truth of closed rounds from exchange fills (dm#844).

Read-only, computed on demand (no new collection). See
:mod:`data_manager.services.exchange_ground_truth` for what a round is and how every
fill and closed row is accounted for.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.services.exchange_ground_truth_inputs import (
    GroundTruthInputError,
    compute_ground_truth,
)
from data_manager.services.exchange_history_client import ExchangeHistoryError

logger = logging.getLogger(__name__)

router = APIRouter()

# Binance keeps the account's trade history for 6 months.
MAX_WINDOW_DAYS = 180


@router.get("/ground-truth")
async def get_ground_truth(
    window_days: float = Query(
        30.0, gt=0, le=MAX_WINDOW_DAYS, description="Window ending now, in days"
    ),
) -> dict[str, Any]:
    """Closed rounds, W/L, P&L and fees per strategy, from the exchange's own fills."""
    try:
        return await compute_ground_truth(api_module.db_manager, window_days)
    except ExchangeHistoryError as exc:
        logger.warning("ground truth: exchange history unavailable: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": "exchange_history_unavailable", "reason": exc.error},
        ) from exc
    except GroundTruthInputError as exc:
        logger.warning("ground truth: input unavailable: %s", exc)
        raise HTTPException(
            status_code=503, detail={"error": "input_unavailable", "reason": str(exc)}
        ) from exc
