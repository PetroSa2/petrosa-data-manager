"""P&L computation endpoint (P4.1, petrosa_k8s#601).

`GET /api/v1/pnl?strategy_id=<id>&scope=strategy|portfolio[&from=<ts>&to=<ts>]`

Reads fills from the `execution_events` audit-trail, replays them through
:class:`data_manager.services.pnl_calculator.PnlCalculator`, marks each
symbol's open lots against the latest fill price, and returns the
realized + unrealized split at the requested scope.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.services.pnl_calculator import PnlCalculator

logger = logging.getLogger(__name__)

router = APIRouter()

SCOPE_STRATEGY = "strategy"
SCOPE_PORTFOLIO = "portfolio"


@router.get("/pnl")
async def get_pnl(
    strategy_id: str | None = Query(
        None,
        description=(
            "Strategy identifier. Required when scope=strategy; ignored "
            "when scope=portfolio."
        ),
    ),
    scope: str = Query(
        SCOPE_STRATEGY,
        description="strategy | portfolio. Defaults to strategy.",
    ),
    from_ts: datetime | None = Query(
        None,
        alias="from",
        description="Start of the replay window (UTC). Inclusive.",
    ),
    to_ts: datetime | None = Query(
        None,
        alias="to",
        description="End of the replay window (UTC). Exclusive.",
    ),
) -> dict:
    if scope not in {SCOPE_STRATEGY, SCOPE_PORTFOLIO}:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be {SCOPE_STRATEGY!r} or {SCOPE_PORTFOLIO!r}",
        )
    if scope == SCOPE_STRATEGY and not strategy_id:
        raise HTTPException(
            status_code=400, detail="strategy_id is required when scope=strategy"
        )

    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    mongodb = getattr(api_module.db_manager, "mongodb_adapter", None)
    if mongodb is None:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    query: dict = {"event_type": {"$in": ["filled", "partial_fill"]}}
    if scope == SCOPE_STRATEGY:
        query["strategy_id"] = strategy_id
    if from_ts is not None or to_ts is not None:
        ts_range: dict = {}
        if from_ts is not None:
            ts_range["$gte"] = from_ts
        if to_ts is not None:
            ts_range["$lt"] = to_ts
        query["timestamp"] = ts_range

    try:
        cursor = mongodb.db["execution_events"].find(query).sort("timestamp", 1)
        rows = await cursor.to_list(length=None)
    except Exception as exc:
        logger.error("pnl: read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="pnl read failed") from exc

    calc = PnlCalculator()
    for row in rows:
        calc.apply_fill(row)

    if scope == SCOPE_STRATEGY:
        breakdown = calc.strategy_pnl(strategy_id)
    else:
        breakdown = calc.portfolio_pnl()

    return {
        "strategy_id": strategy_id if scope == SCOPE_STRATEGY else None,
        "scope": scope,
        "realized": breakdown.realized,
        "unrealized": breakdown.unrealized,
        "total": breakdown.total,
        "positions": calc.position_summary(),
        "fills_replayed": len(rows),
    }
