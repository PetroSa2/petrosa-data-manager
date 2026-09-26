"""Typed Mongo-backed positions and daily P&L API."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, date, datetime, timezone
from functools import wraps
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import DuplicateKeyError

import data_manager.api.app as api_module
from data_manager.db.repositories.trading_state_repository import TradingStateRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/trading")
_copy_tasks: set[asyncio.Task] = set()


class TradingDocument(BaseModel):
    model_config = ConfigDict(extra="allow")
    position_id: str | None = None


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
        except Exception as exc:
            logger.exception("trading_state_database_error")
            raise HTTPException(
                status_code=503, detail="Trading state database unavailable"
            ) from exc

    return wrapped


def _schedule_mysql_copy(operation: str, document: dict[str, Any]) -> None:
    if os.getenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true").lower() != "true":
        return
    task = asyncio.create_task(asyncio.to_thread(_copy_to_mysql, operation, document))
    _copy_tasks.add(task)
    task.add_done_callback(_copy_tasks.discard)


def _copy_to_mysql(operation: str, document: dict[str, Any]) -> None:
    try:
        manager = api_module.db_manager
        adapter = getattr(manager, "mysql_adapter", None) if manager else None
        if adapter is None:
            return
        table = "daily_pnl" if operation == "daily_pnl" else "positions"
        allowed = set(adapter.get_column_names(table))
        data = {k: v for k, v in document.items() if k in allowed}
        key = "date" if table == "daily_pnl" else "position_id"
        updated = adapter.update(table, {key: document[key]}, data)
        if not updated:
            from pydantic import create_model

            adapter.write(
                [
                    create_model(
                        "TradingStateRow", **{k: (Any, v) for k, v in data.items()}
                    )()
                ],
                table,
            )
    except Exception:
        logger.exception(
            "trading_state_mysql_copy_failed", extra={"operation": operation}
        )


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
    _schedule_mysql_copy("positions", document)
    return {"position_id": body.position_id, "created": True}


@router.put("/positions/{position_id}")
@_database_errors_as_503
async def replace_position(position_id: str, body: TradingDocument):
    repo = _repo()
    current = await repo.get_position(position_id)
    data = body.model_dump(exclude_none=True)
    if current and current.get("status") == "closed" and data.get("status") == "open":
        raise HTTPException(status_code=409, detail="cannot reopen closed position")
    data["position_id"] = position_id
    await repo.update_position(position_id, data, upsert=True)
    _schedule_mysql_copy("positions", data)
    return data


@router.patch("/positions/{position_id}")
@_database_errors_as_503
async def patch_position(position_id: str, body: TradingDocument):
    repo = _repo()
    if not await repo.get_position(position_id):
        raise HTTPException(status_code=404, detail="position not found")
    data = body.model_dump(exclude_none=True)
    data.pop("position_id", None)
    await repo.update_position(position_id, data)
    _schedule_mysql_copy("positions", {"position_id": position_id, **data})
    return {"position_id": position_id, **data}


@router.post("/positions/{position_id}/close")
@_database_errors_as_503
async def close_position(position_id: str, body: TradingDocument):
    repo = _repo()
    data = body.model_dump(exclude_none=True)
    if not await repo.close_position(position_id, data):
        raise HTTPException(status_code=409, detail="position is not open")
    _schedule_mysql_copy(
        "positions", {"position_id": position_id, **data, "status": "closed"}
    )
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
        _schedule_mysql_copy(
            "positions", {"position_id": position_id, **update, "status": "closed"}
        )
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
    _schedule_mysql_copy("daily_pnl", data)
    return result
