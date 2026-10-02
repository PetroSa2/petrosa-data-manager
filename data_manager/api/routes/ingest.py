"""Typed ingest endpoints owned by data-manager."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import AliasChoices, BaseModel, Field
from pymongo import UpdateOne

import constants
from data_manager.db.repositories.candle_repository import (
    candle_to_mysql_kline,
    map_mongo_kline_doc,
    mysql_table_name,
)
from data_manager.db.repositories.funding_repository import FundingRepository
from data_manager.maintenance.candle_sanity import CANDLE_SANITY_VIOLATIONS
from data_manager.models.events import BackfillRequest
from data_manager.models.market_data import Candle, FundingRate
from data_manager.utils.time_utils import parse_timeframe_to_seconds

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
    klines: list[dict[str, Any]] = Field(
        validation_alias=AliasChoices("klines", "data")
    )


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
        logger.error("klines_mysql_copy_failed", exc_info=True)
    else:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="success").inc()


def _schedule_copy(rows: list, interval: str, adapter: Any) -> None:
    task = asyncio.create_task(_copy(rows, interval, adapter))
    _mysql_copy_tasks.add(task)
    task.add_done_callback(_mysql_copy_tasks.discard)


async def _quarantine_and_refetch(
    documents: list[tuple[dict[str, Any], list[str]]],
) -> None:
    """Keep rejected payloads for audit and ask the Binance backfill path to retry."""
    if not documents or db_manager is None:
        return
    mongo = getattr(db_manager, "mongodb_adapter", None)
    if mongo is not None:
        try:
            collection = mongo.db["candle_quarantine"]
            await collection.insert_many(
                [
                    {
                        **document,
                        "quarantine_reasons": reasons,
                        "quarantined_at": datetime.now(UTC),
                    }
                    for document, reasons in documents
                ],
                ordered=False,
            )
        except Exception:
            logger.warning("Unable to persist candle quarantine records", exc_info=True)

    orchestrator = getattr(db_manager, "backfill_orchestrator", None)
    if orchestrator is None:
        return
    for document, _ in documents:
        try:
            start = _parse_timestamp(document["timestamp"])
            interval = str(document["interval"])
            await orchestrator.create_backfill_job(
                BackfillRequest(
                    symbol=str(document["symbol"]),
                    data_type="candles",
                    timeframe=interval,
                    start_time=start,
                    end_time=start
                    + timedelta(seconds=parse_timeframe_to_seconds(interval)),
                    priority=1,
                    source="candle_sanity_refetch",
                )
            )
        except Exception:
            logger.warning("Unable to schedule candle sanity refetch", exc_info=True)


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
    rejected_documents: list[tuple[dict[str, Any], list[str]]] = []
    rejected = 0
    seen_timestamps: set[tuple[str, datetime]] = set()
    for raw in request.klines:
        document = {**raw, "symbol": request.symbol, "interval": request.interval}
        try:
            document["timestamp"] = _parse_timestamp(document.get("timestamp"))
            key = (request.symbol, document["timestamp"])
            if key in seen_timestamps:
                CANDLE_SANITY_VIOLATIONS.labels(
                    symbol=request.symbol,
                    timeframe=request.interval,
                    reason="duplicate_timestamp",
                ).inc()
                rejected += 1
                rejected_documents.append((document, ["duplicate_timestamp"]))
                continue
            seen_timestamps.add(key)
            mapped = map_mongo_kline_doc(document)
            if mapped is None:
                rejected += 1
                rejected_documents.append((document, ["sanity_violation"]))
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
            rejected_documents.append((document, ["invalid_payload"]))
            continue
        accepted.append((document, candle))

    await _quarantine_and_refetch(rejected_documents)

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
    elif copy_status == "unavailable" and accepted:
        KLINES_MYSQL_COPY.labels(interval=request.interval, outcome="unavailable").inc()
        logger.error(
            "klines_mysql_copy_unavailable: Mongo write succeeded but no MySQL "
            "adapter is configured; historic completeness is at risk",
            extra={"symbol": request.symbol, "interval": request.interval},
        )
    return {
        "symbol": request.symbol,
        "interval": request.interval,
        "received": len(request.klines),
        "rejected": rejected,
        "quarantined": len(rejected_documents),
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
