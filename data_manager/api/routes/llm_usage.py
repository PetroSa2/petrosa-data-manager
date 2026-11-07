"""LLM usage from the gateway's call records (petrosa-data-manager#841).

``GET /api/v1/llm/usage?group_by=caller|model_alias|decision_id|day&from=&to=&caller=&model_alias=&decision_id=``
answers per group: attempts, requests, prompt/completion tokens, latency and queue-wait p50/p95, and outcome
counts. One server-side aggregation, bounded at 2 s; a slower query answers 503 with the reason, never a
partial result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.services import llm_call_records

router = APIRouter(prefix="/api/v1/llm", tags=["LLM usage"])


@router.get("/usage")
async def get_llm_usage(
    group_by: str = Query("caller", pattern="^(caller|model_alias|decision_id|day)$"),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    caller: str | None = Query(None, max_length=64),
    model_alias: str | None = Query(None, max_length=64),
    decision_id: str | None = Query(None, max_length=128),
) -> dict[str, Any]:
    end = to or datetime.now(UTC)
    start = from_ or end - timedelta(days=1)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    manager = api_module.db_manager
    adapter = getattr(manager, "mongodb_adapter", None) if manager else None
    if adapter is None or getattr(adapter, "db", None) is None:
        raise HTTPException(status_code=503, detail={"error": "mongodb_unavailable"})
    try:
        return await llm_call_records.usage(
            adapter,
            group_by,
            start,
            end,
            {"caller": caller, "model_alias": model_alias, "decision_id": decision_id},
        )
    except llm_call_records.UsageQueryTimeout as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "usage_query_timeout", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
