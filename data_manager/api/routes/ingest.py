"""Typed ingest endpoints owned by data-manager."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel
from pymongo import UpdateOne

import constants
from data_manager.db.repositories.candle_repository import (
    candle_to_mysql_kline,
    map_mongo_kline_doc,
    mysql_table_name,
)
from data_manager.db.repositories.funding_repository import FundingRepository
from data_manager.models.market_data import Candle, FundingRate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ingest", tags=["Ingest"])
db_manager: Any = None
_mysql_copy_tasks: set[asyncio.Task] = set()
KLINES_MYSQL_COPY = Counter(
    "data_manager_klines_mysql_copy_total",
    "Outcomes of kline MySQL copies",
    ["interval", "outcome"],
)


class KlinesRequest(BaseModel):
    symbol: str
    interval: str
    klines: list[dict[str, Any]]


class FundingRequest(BaseModel):
    symbol: str
    rates: list[dict[str, Any]]


def set_database_manager(manager: Any) -> None:
    global db_manager
    db_manager = manager


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp must be an ISO datetime")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _copy(rows: list, interval: str, adapter: Any) -> None:
    try:
        await asyncio.to_thread(adapter.write_batch, rows, mysql_table_name(interval))
    except Exception:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="error").inc()
        logger.warning("klines_mysql_copy_failed", exc_info=True)
    else:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="success").inc()


def _schedule_copy(rows: list, interval: str, adapter: Any) -> None:
    task = asyncio.create_task(_copy(rows, interval, adapter))
    _mysql_copy_tasks.add(task)
    task.add_done_callback(_mysql_copy_tasks.discard)


@router.post("/klines")
async def ingest_klines(request: KlinesRequest) -> dict[str, Any]:
    if request.interval not in constants.SUPPORTED_INTERVALS:
        raise HTTPException(status_code=422, detail="unsupported interval")
    if len(request.klines) > constants.API_MAX_BATCH_SIZE:
        raise HTTPException(status_code=413, detail="kline batch exceeds maximum size")
    mongo = getattr(db_manager, "mongodb_adapter", None) if db_manager else None
    mysql = getattr(db_manager, "mysql_adapter", None) if db_manager else None
    if mongo is None:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")

    accepted: list[tuple[dict[str, Any], Candle]] = []
    rejected = 0
    for raw in request.klines:
        document = {**raw, "symbol": request.symbol, "interval": request.interval}
        try:
            document["timestamp"] = _parse_timestamp(document.get("timestamp"))
            mapped = map_mongo_kline_doc(document)
            if mapped is None:
                rejected += 1
                continue
            candle = Candle(
                symbol=request.symbol,
                timestamp=document["timestamp"],
                timeframe=request.interval,
                open=mapped["open"],
                high=mapped["high"],
                low=mapped["low"],
                close=mapped["close"],
                volume=mapped["volume"],
                quote_volume=mapped["quote_volume"],
                trades_count=mapped["trades_count"],
            )
        except (TypeError, ValueError, ArithmeticError):
            rejected += 1
            continue
        accepted.append((document, candle))

    operations = [
        UpdateOne(
            {"symbol": request.symbol, "timestamp": doc["timestamp"]},
            {"$set": doc},
            upsert=True,
        )
        for doc, _ in accepted
    ]
    result = (
        await mongo.db[f"klines_{request.interval}"].bulk_write(
            operations, ordered=False
        )
        if operations
        else None
    )
    upserted = int(getattr(result, "upserted_count", 0)) if result else 0
    matched = int(getattr(result, "matched_count", 0)) if result else 0
    copy_status = (
        "disabled"
        if os.getenv("PETROSA_KLINES_MYSQL_COPY_ENABLED", "true").lower() != "true"
        else "unavailable"
    )
    if copy_status != "disabled" and mysql is not None and accepted:
        _schedule_copy(
            [candle_to_mysql_kline(candle) for _, candle in accepted],
            request.interval,
            mysql,
        )
        copy_status = "scheduled"
    return {
        "symbol": request.symbol,
        "interval": request.interval,
        "received": len(request.klines),
        "rejected": rejected,
        "upserted": upserted,
        "matched": matched,
        "mysql_copy": copy_status,
    }


@router.post("/funding")
async def ingest_funding(request: FundingRequest) -> dict[str, Any]:
    mongo = getattr(db_manager, "mongodb_adapter", None) if db_manager else None
    mysql = getattr(db_manager, "mysql_adapter", None) if db_manager else None
    if mongo is None:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    rates = []
    for raw in request.rates:
        try:
            timestamp = raw.get("timestamp", raw.get("funding_time"))
            if timestamp is None:
                raise ValueError("funding timestamp is required")
            rates.append(
                FundingRate(
                    symbol=request.symbol,
                    timestamp=_parse_timestamp(timestamp),
                    funding_rate=Decimal(str(raw["funding_rate"])),
                    mark_price=Decimal(str(raw["mark_price"]))
                    if raw.get("mark_price") is not None
                    else None,
                )
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    inserted = await FundingRepository(mysql, mongo).insert_batch(rates)
    return {"symbol": request.symbol, "received": len(rates), "inserted": inserted}
