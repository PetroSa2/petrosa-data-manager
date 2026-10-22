"""Per-strategy realized performance report."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import constants
import data_manager.api.app as api_module
from data_manager.models.execution_event import KNOWN_EVENT_TYPES
from data_manager.services.strategy_performance import build_strategy_performance

router = APIRouter()


@router.get("/strategy/performance")
async def get_strategy_performance(
    from_: datetime = Query(
        ..., alias="from", description="Inclusive UTC window start"
    ),
    to: datetime = Query(..., description="Exclusive UTC window end"),
) -> dict[str, Any]:
    if from_ >= to:
        raise HTTPException(status_code=422, detail="from must be before to")
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    from data_manager.services.historic_analytics import get_mysql_adapter

    mysql = get_mysql_adapter(api_module.db_manager)
    mongodb = getattr(api_module.db_manager, "mongodb_adapter", None)
    if mongodb is None and mysql is None:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    try:
        if mysql is not None:
            from data_manager.services.historic_analytics import read_execution_events

            rows, as_of = await asyncio.wait_for(
                asyncio.to_thread(
                    read_execution_events,
                    mysql,
                    start=from_,
                    end=to,
                    limit=50_000,
                    event_types=set(KNOWN_EVENT_TYPES),
                ),
                constants.ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS,
            )
            strategy_ids = {
                str(row["strategy_id"]).strip()
                for row in rows
                if row.get("strategy_id")
            }
            from data_manager.services.historic_analytics import read_table

            strategy_rows = await asyncio.wait_for(
                asyncio.to_thread(read_table, mysql, "cio_decisions"),
                constants.ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS,
            )
            strategy_ids.update(
                str(row["strategy_id"]).strip()
                for row in strategy_rows
                if row.get("strategy_id")
            )
            source = "execution_events_mysql"
        else:
            query = {
                "event_type": {"$in": sorted(KNOWN_EVENT_TYPES)},
                "timestamp": {"$gte": from_, "$lt": to},
            }
            cursor = mongodb.db["execution_events"].find(query).sort("timestamp", 1)
            rows = await cursor.to_list(length=50_000)
            as_of = None
            strategy_ids = {
                str(row["strategy_id"]).strip()
                for row in rows
                if row.get("strategy_id")
            }
            source = "execution_events"
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "unavailable",
                "reason": "strategy performance source timed out",
                "source_errors": {"execution_events": "timeout"},
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "unavailable",
                "reason": "strategy performance source failed",
                "source_errors": {"execution_events": str(exc)},
            },
        ) from exc

    strategies = build_strategy_performance(
        rows, start=from_, end=to, strategy_ids=strategy_ids
    )
    if not strategies:
        return {
            "status": "incomplete",
            "reason": "no usable strategy history or strategy catalog",
            "source_errors": {"execution_events": "no usable rows"},
            "strategies": [],
            "from": from_.isoformat(),
            "to": to.isoformat(),
            "generated_at": datetime.now(UTC).isoformat(),
            "source": source,
            "as_of": as_of,
        }
    for strategy in strategies:
        sample_size = int(strategy["trade_count"])
        strategy["sample_size"] = sample_size
        strategy["sample_status"] = "sampled" if sample_size else "zero_sample"
        strategy["uncertainty"] = {
            "status": "measured" if sample_size >= 2 else "insufficient_sample",
            "win_rate": None,
            "net_r_total": None,
        }
    return {
        "status": "complete",
        "strategies": strategies,
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "as_of": as_of,
    }
