"""Typed Mongo-backed positions and daily P&L API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, date, datetime, timezone
from functools import wraps
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo.errors import DuplicateKeyError

import data_manager.api.app as api_module
from data_manager.db.repositories.trading_state_repository import (
    TradingStateConflictError,
    TradingStateRepository,
)
from data_manager.observability_metrics import (
    TRADING_STATE_DEAD_LETTER_COUNT,
    TRADING_STATE_MYSQL_COPY_FAILED,
    TRADING_STATE_MYSQL_FIELDS_DROPPED,
    TRADING_STATE_RECOVERY_EVENTS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/trading")
_copy_tasks: set[asyncio.Task] = set()
_copy_state: dict[str, Any] = {
    "pending": 0,
    "failed": 0,
    "dead_lettered": 0,
    "fallback_failed": 0,
    "replayed": 0,
    "task_failed": 0,
    "last_failure_time": None,
}
_COPY_RETRIES = 3
_COPY_BACKOFF_SECONDS = 0.01
_COPY_BATCH_SIZE = 25
_COPY_POLL_SECONDS = 5
_VALID_POSITION_STATUSES = frozenset(
    {"open", "partially_closed", "closed", "superseded"}
)
_DROPPED_FIELD_ALLOWLIST = frozenset(
    {
        "entry_commission",
        "pnl_unknown",
        "fee_status",
        "closed_by_strategy_id",
        "status",
    }
)


class TradingDocument(BaseModel):
    model_config = ConfigDict(extra="allow")
    position_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_internal_fields(cls, value):
        if isinstance(value, dict) and any(
            key == "_id" or key.startswith("_mysql_copy_") for key in value
        ):
            raise ValueError("reserved trading state metadata")
        return value


class DailyPnlRequest(BaseModel):
    daily_pnl: float


def _repo() -> TradingStateRepository:
    manager = api_module.db_manager
    if not manager or not manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")
    return TradingStateRepository(manager.mysql_adapter, manager.mongodb_adapter)


def _database_errors_as_503(handler):
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except HTTPException:
            raise
        except TradingStateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("trading_state_database_error")
            raise HTTPException(
                status_code=503, detail="Trading state database unavailable"
            ) from exc

    return wrapped


def _schedule_mysql_copy() -> None:
    if os.getenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true").lower() != "true":
        return
    if _copy_tasks:
        return
    _copy_state["pending"] += 1
    task = asyncio.create_task(_replay_mysql_copies())
    _copy_tasks.add(task)

    def _complete(done: asyncio.Task) -> None:
        _copy_tasks.discard(done)
        _copy_state["pending"] = max(0, _copy_state["pending"] - 1)
        if not done.cancelled() and done.exception() is not None:
            _record_task_failure()

    task.add_done_callback(_complete)


def _record_task_failure() -> None:
    _copy_state["task_failed"] += 1
    TRADING_STATE_RECOVERY_EVENTS.add(1, {"outcome": "task_failed"})
    logger.error("trading_state_mysql_replay_task_failed")


async def _replay_mysql_copies() -> None:
    try:
        repo = _repo()
        for table in ("positions", "daily_pnl"):
            for document in await repo.pending_copies(table, _COPY_BATCH_SIZE):
                if await asyncio.to_thread(_copy_to_mysql, table, document):
                    await repo.acknowledge_copy(table, document)
                    _copy_state["replayed"] += 1
                    TRADING_STATE_RECOVERY_EVENTS.add(1, {"outcome": "replayed"})
                else:
                    await repo.defer_copy(table, document)
    except asyncio.CancelledError:
        raise
    except Exception:
        _record_task_failure()


async def mysql_copy_worker() -> None:
    try:
        while True:
            _schedule_mysql_copy()
            if _copy_tasks:
                await asyncio.gather(*_copy_tasks, return_exceptions=True)
            await asyncio.sleep(_COPY_POLL_SECONDS)
    finally:
        if _copy_tasks:
            await asyncio.gather(*_copy_tasks, return_exceptions=True)


def _record_failure(operation: str, reason: str) -> None:
    _copy_state["failed"] += 1
    _copy_state["last_failure_time"] = datetime.now(UTC).isoformat()
    TRADING_STATE_MYSQL_COPY_FAILED.add(1, {"operation": operation, "reason": reason})


def _dead_letter(
    adapter: Any, operation: str, document: dict[str, Any], reason: str
) -> None:
    payload = {
        "operation": operation,
        "reason": reason,
        "payload": document,
        "created_at": datetime.now(UTC),
    }
    try:
        from pydantic import create_model

        row = create_model(
            "TradingStateDeadLetter",
            operation=(str, operation),
            reason=(str, reason),
            payload=(
                dict[str, Any],
                jsonable_encoder({k: v for k, v in document.items() if k != "_id"}),
            ),
            created_at=(datetime, payload["created_at"]),
        )()
        result = adapter.write([row], "trading_state_mysql_dead_letters")
        if result != 1 or getattr(result, "failed", 0):
            raise RuntimeError("dead-letter persistence not confirmed")
        _copy_state["dead_lettered"] += 1
        TRADING_STATE_DEAD_LETTER_COUNT.set(_copy_state["dead_lettered"])
    except Exception:
        _copy_state["fallback_failed"] += 1
        TRADING_STATE_RECOVERY_EVENTS.add(1, {"outcome": "fallback_failed"})
        logger.error(
            "trading_state_mysql_dead_letter_failed",
            extra={"operation": operation, "reason": "write_error"},
        )


def _copy_to_mysql(operation: str, document: dict[str, Any]) -> bool:
    manager = api_module.db_manager
    adapter = getattr(manager, "mysql_adapter", None) if manager else None
    if adapter is None:
        _record_failure(operation, "adapter_missing")
        _dead_letter(adapter, operation, document, "adapter_missing")
        return False
    table = "daily_pnl" if operation == "daily_pnl" else "positions"
    if table == "positions" and document.get("status") not in (
        None,
        *_VALID_POSITION_STATUSES,
    ):
        _record_failure(operation, "invalid_value")
        _dead_letter(adapter, operation, document, "invalid_value")
        return False
    for attempt in range(_COPY_RETRIES):
        try:
            allowed = set(adapter.get_column_names(table))
            dropped = set(TradingStateRepository._public(document)) - allowed
            for field in dropped:
                TRADING_STATE_MYSQL_FIELDS_DROPPED.add(
                    1,
                    {
                        "table": table,
                        "field": field
                        if field in _DROPPED_FIELD_ALLOWLIST
                        else "other",
                    },
                )
            data = {k: v for k, v in document.items() if k in allowed}
            key = "date" if table == "daily_pnl" else "position_id"
            if key not in data:
                raise ValueError("missing logical key")
            adapter.persist_trading_state(table, data, document["_mysql_copy_version"])
            return True
        except Exception:
            if attempt + 1 < _COPY_RETRIES:
                time.sleep(_COPY_BACKOFF_SECONDS * (attempt + 1))
                continue
            _record_failure(operation, "copy_error")
            logger.error(
                "trading_state_mysql_copy_failed",
                extra={"operation": operation, "reason": "copy_error"},
            )
            _dead_letter(adapter, operation, document, "copy_error")
    return False


@router.get("/mysql-copy/status")
async def mysql_copy_status():
    repo = _repo()
    pending_summary = await repo.pending_copy_summary()
    return {
        **_copy_state,
        "durable_pending": pending_summary["positions"] + pending_summary["daily_pnl"],
        "durable_pending_by_table": {
            "positions": pending_summary["positions"],
            "daily_pnl": pending_summary["daily_pnl"],
        },
        "durable_oldest_attempted_at": pending_summary["oldest_attempted_at"],
        "retry_state": "mongo_pending_version; restart_resumable",
        "batch_size_per_table": _COPY_BATCH_SIZE,
        "concurrency": 1,
        "attempts_per_copy": _COPY_RETRIES,
    }


@router.post("/positions", status_code=201)
@_database_errors_as_503
async def create_position(body: TradingDocument):
    if not body.position_id:
        raise HTTPException(status_code=422, detail="position_id is required")
    document = body.model_dump(exclude_none=True)
    repo = _repo()
    try:
        await repo.create_position(document)
    except DuplicateKeyError:
        return {"position_id": body.position_id, "created": False, "duplicate": True}
    _schedule_mysql_copy()
    return {"position_id": body.position_id, "created": True}


@router.put("/positions/{position_id}")
@_database_errors_as_503
async def replace_position(position_id: str, body: TradingDocument):
    repo = _repo()
    current = await repo.get_position(position_id)
    data = body.model_dump(exclude_none=True)
    if current and current.get("status") == "closed" and data.get("status") == "open":
        raise HTTPException(status_code=409, detail="cannot reopen closed position")
    await repo.update_position(position_id, data, upsert=True)
    _schedule_mysql_copy()
    return {**data, "position_id": position_id}


@router.patch("/positions/{position_id}")
@_database_errors_as_503
async def patch_position(position_id: str, body: TradingDocument):
    repo = _repo()
    if not await repo.get_position(position_id):
        raise HTTPException(status_code=404, detail="position not found")
    data = body.model_dump(exclude_none=True)
    await repo.update_position(position_id, data)
    _schedule_mysql_copy()
    return {"position_id": position_id, **data}


@router.post("/positions/{position_id}/close")
@_database_errors_as_503
async def close_position(position_id: str, body: TradingDocument):
    repo = _repo()
    data = body.model_dump(exclude_none=True)
    if not await repo.close_position(position_id, data):
        raise HTTPException(status_code=409, detail="position is not open")
    _schedule_mysql_copy()
    return {"closed": True}


@router.post("/positions/close-by-side")
@_database_errors_as_503
async def close_by_side(body: TradingDocument):
    data = body.model_dump(exclude_none=True)
    symbol, side, update = (
        data.pop("symbol", None),
        data.pop("position_side", None),
        data.pop("update", {}),
    )
    if not symbol or not side:
        raise HTTPException(
            status_code=422, detail="symbol and position_side are required"
        )
    repo = _repo()
    position_id = await repo.close_by_side(symbol, side, update)
    if position_id:
        _schedule_mysql_copy()
    return {"closed": bool(position_id), "position_id": position_id}


@router.get("/positions/{position_id}")
@_database_errors_as_503
async def get_position(position_id: str):
    doc = await _repo().get_position(position_id)
    if not doc:
        raise HTTPException(status_code=404, detail="position not found")
    return doc


@router.get("/positions")
@_database_errors_as_503
async def list_positions(
    status: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    limit: int = Query(500, le=500),
):
    filters: dict[str, Any] = {}
    if status:
        values = [item.strip() for item in status.split(",") if item.strip()]
        filters["status"] = values[0] if len(values) == 1 else {"$in": values}
    if strategy_id:
        filters["strategy_id"] = strategy_id
    if symbol:
        filters["symbol"] = symbol
    rows = await _repo().list_positions(filters, limit)
    return {"data": rows, "count": len(rows)}


@router.get("/daily-pnl/{date_value}")
@_database_errors_as_503
async def get_daily_pnl(date_value: date):
    doc = await _repo().get_daily_pnl(date_value.isoformat())
    if not doc:
        raise HTTPException(status_code=404, detail="daily P&L not found")
    return doc


@router.put("/daily-pnl/{date_value}")
@_database_errors_as_503
async def put_daily_pnl(date_value: date, body: DailyPnlRequest):
    data = {
        "date": date_value.isoformat(),
        "daily_pnl": body.daily_pnl,
        "updated_at": datetime.now(UTC),
    }
    result = await _repo().put_daily_pnl(date_value.isoformat(), data)
    _schedule_mysql_copy()
    return result
