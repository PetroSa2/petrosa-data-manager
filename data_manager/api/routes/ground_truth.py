"""Per-strategy ground truth of closed rounds from exchange fills (dm#844).

Read-only, computed on demand (no new collection). See
:mod:`data_manager.services.exchange_ground_truth` for what a round is and how every
fill and closed row is accounted for.

A request has a total time budget, so a wide window can't hold a request worker for
many minutes. The exchange reads fan out to about ``pairs x 2 x ceil(window / 7 days)``
calls, paced by tradeengine's rate guard. A window whose estimate exceeds the ceiling
is refused up front; otherwise the computation runs under ``asyncio.timeout``. Both
answer 504 ``ground_truth_time_budget`` with a hint to use a smaller window.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import constants
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
# Tradeengine's rate guard for these reads (PetroSa2/petrosa-tradeengine#833):
# 10% of the 2400/min account weight at 5 per call.
GUARD_CALLS_PER_MINUTE = 48
# Each symbol needs a fills and an orders read per 7-day span.
CALLS_PER_SYMBOL_SPAN = 2
SPAN_DAYS = 7
# Margin over the paced estimate, and fixed overhead for the store reads and build.
BUDGET_MARGIN = 1.5
BUDGET_OVERHEAD_SECONDS = 30.0
# Policy ceiling: no request holds a data-manager worker longer than this.
BUDGET_CEILING_SECONDS = 300.0


def time_budget_seconds(window_days: float, symbols: int) -> float:
    """The paced time this window needs, with margin; may exceed the ceiling."""
    spans = math.ceil(window_days / SPAN_DAYS)
    calls = max(1, symbols) * CALLS_PER_SYMBOL_SPAN * spans
    return (
        BUDGET_OVERHEAD_SECONDS + BUDGET_MARGIN * calls * 60.0 / GUARD_CALLS_PER_MINUTE
    )


def _over_budget(window_days: float, budget: float) -> HTTPException:
    return HTTPException(
        status_code=504,
        detail={
            "error": "ground_truth_time_budget",
            "window_days": window_days,
            "budget_seconds": round(budget, 1),
            "hint": "use a smaller window",
        },
    )


@router.get("/ground-truth")
async def get_ground_truth(
    window_days: float = Query(
        30.0, gt=0, le=MAX_WINDOW_DAYS, description="Window ending now, in days"
    ),
) -> dict[str, Any]:
    """Closed rounds, W/L, P&L and fees per strategy, from the exchange's own fills."""
    budget = time_budget_seconds(window_days, len(constants.SUPPORTED_PAIRS))
    if budget > BUDGET_CEILING_SECONDS:
        raise _over_budget(window_days, budget)
    try:
        async with asyncio.timeout(budget):
            return await compute_ground_truth(api_module.db_manager, window_days)
    except TimeoutError as exc:
        logger.warning(
            "ground truth: window_days=%s exceeded its %.0f s budget",
            window_days,
            budget,
        )
        raise _over_budget(window_days, budget) from exc
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
