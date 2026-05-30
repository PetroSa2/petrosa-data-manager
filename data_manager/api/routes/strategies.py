"""Strategy-registry HTTP routes (#195, FR54).

Three endpoints mounted under `/api/strategies`:

* `POST /api/strategies` — persist a candidate strategy submission.
* `GET  /api/strategies/{strategy_id}` — exact-id lookup.
* `GET  /api/strategies` — paged list with optional `status` filter.

**Security note:** the route layer does NOT execute, import, compile, or
otherwise evaluate the submitted `code` field. It is persisted verbatim.
Code execution and sandboxing are a separate concern handled CLI-side in
`petrosa-bot-ta-analysis#255` after threat-model sign-off.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

import data_manager.api.app as api_module
from data_manager.db.repositories.strategy_registry_repository import (
    StrategyAlreadyRegisteredError,
    StrategyRegistryRepository,
)
from data_manager.models.registered_strategy import (
    CreateStrategyRequest,
    RegisteredStrategy,
    StrategyStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_mongo():  # type: ignore[no-untyped-def]
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "title": "MongoDB unavailable",
                "detail": "data-manager is not connected to MongoDB",
            },
        )
    return api_module.db_manager.mongodb_adapter


def _repo() -> StrategyRegistryRepository:
    return StrategyRegistryRepository(mongodb_adapter=_require_mongo())


@router.post("/api/strategies", status_code=201)
async def register_strategy(req: CreateStrategyRequest) -> dict[str, Any]:
    """Persist a candidate strategy submission.

    Returns `{strategy_id, registered_at, status}` per the AC contract.
    Returns 409 if `strategy_id` is already registered.
    """
    candidate = RegisteredStrategy(
        strategy_id=req.strategy_id,
        code=req.code,
        parameter_set=req.parameter_set,
        symbol_scope=req.symbol_scope,
        submitted_by=req.submitted_by,
        signed_action_id=req.signed_action_id,
    )
    try:
        registered = await _repo().insert(candidate)
    except StrategyAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "title": "strategy already registered",
                "detail": str(exc),
                "strategy_id": exc.strategy_id,
            },
        ) from exc
    return {
        "strategy_id": registered.strategy_id,
        "registered_at": registered.registered_at.isoformat(),
        "status": registered.status,
    }


@router.get("/api/strategies/{strategy_id}")
async def get_strategy(
    strategy_id: str = Path(..., min_length=1, max_length=256),
) -> dict[str, Any]:
    """Exact `strategy_id` lookup; 404 if missing."""
    strategy = await _repo().get(strategy_id)
    if strategy is None:
        raise HTTPException(
            status_code=404,
            detail={
                "title": "strategy not registered",
                "detail": f"no document with strategy_id={strategy_id!r}",
            },
        )
    return strategy.model_dump(mode="json")


@router.get("/api/strategies")
async def list_strategies(
    status: StrategyStatus | None = Query(
        default=None,
        description="Filter by lifecycle status (candidate|accepted|rejected).",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Paged list, newest-first, optionally filtered by `status`."""
    strategies = await _repo().list(status=status, limit=limit, offset=offset)
    return {
        "strategies": [s.model_dump(mode="json") for s in strategies],
        "count": len(strategies),
        "limit": limit,
        "offset": offset,
        "status_filter": status,
    }
