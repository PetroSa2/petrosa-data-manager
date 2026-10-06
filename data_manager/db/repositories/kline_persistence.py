"""The one place klines are persisted: the Mongo write and its durable MySQL copy.

Every path that puts klines into MongoDB (the extractor ingest route, the backfill orchestrator through
``CandleRepository``) calls :func:`persist_klines`, so no Mongo-only kline write path exists. Before this,
data-manager's own backfill wrote ``klines_1h``/``klines_1d`` to Mongo only, got ahead of the extractor,
the extractor then skipped every symbol and the MySQL copy starved (petrosa-data-manager#526).

The MySQL copy is insert-only on ``(symbol, timestamp)``: an existing row is never changed, and the outcome
is counted in ``data_manager_klines_mysql_copy_total``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter
from pymongo import UpdateOne

from data_manager.db.repositories.candle_repository import (
    candle_to_mongo_kline,
    candle_to_mysql_kline,
    map_mongo_kline_doc,
    mysql_table_name,
)
from data_manager.models.market_data import Candle

logger = logging.getLogger(__name__)

#: A kline is one row per symbol and open time; the MySQL copy matches duplicates on this pair.
KLINE_NATURAL_KEY = ("symbol", "timestamp")

KLINES_MYSQL_COPY = Counter(
    "data_manager_klines_mysql_copy_total",
    "Outcomes of kline MySQL copies",
    ["interval", "outcome"],
)

_copy_tasks: set[asyncio.Task] = set()


@dataclass
class KlinePersistResult:
    """What one :func:`persist_klines` call did."""

    #: Mongo documents created (``overwrite=True``: upserts; ``overwrite=False``: inserts).
    upserted: int = 0
    #: Mongo documents rejected because their natural key already existed.
    duplicates: int = 0
    #: Mongo documents that already existed and were replaced (``overwrite=True`` only).
    matched: int = 0
    #: ``scheduled``, ``copied``, ``disabled`` or ``unavailable``.
    mysql_copy: str = "unavailable"


def mysql_copy_enabled() -> bool:
    return os.getenv("PETROSA_KLINES_MYSQL_COPY_ENABLED", "true").lower() == "true"


async def copy_rows_to_mysql(rows: list, interval: str, adapter: Any) -> None:
    """Insert-only MySQL copy; best effort, counted and logged, never raises."""
    try:
        await asyncio.to_thread(
            adapter.write_batch,
            rows,
            mysql_table_name(interval),
            insert_only=True,
            natural_key=KLINE_NATURAL_KEY,
        )
    except Exception:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="error").inc()
        logger.error("klines_mysql_copy_failed", exc_info=True)
    else:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="success").inc()


def _schedule_copy(rows: list, interval: str, adapter: Any) -> None:
    task = asyncio.create_task(copy_rows_to_mysql(rows, interval, adapter))
    _copy_tasks.add(task)
    task.add_done_callback(_copy_tasks.discard)


async def persist_klines(
    mongo: Any,
    mysql: Any,
    interval: str,
    entries: list[tuple[dict[str, Any] | None, Candle]],
    *,
    overwrite: bool,
    wait_for_copy: bool = False,
) -> KlinePersistResult:
    """Write klines to Mongo, then copy them to MySQL.

    ``entries`` are ``(document, candle)`` pairs. ``overwrite=True`` upserts the ``document`` on
    ``(symbol, timestamp)`` (the extractor is the authority for what it sends). ``overwrite=False`` only
    inserts, so another writer's document is never replaced (the backfill), and the document is built from
    the candle. The copy runs in the background unless ``wait_for_copy`` is set.
    """
    result = KlinePersistResult(
        mysql_copy="disabled" if not mysql_copy_enabled() else "unavailable"
    )
    if not entries:
        return result

    collection = f"klines_{interval}"
    if overwrite:
        operations = [
            UpdateOne(
                {"symbol": candle.symbol, "timestamp": _timestamp(document, candle)},
                {"$set": document},
                upsert=True,
            )
            for document, candle in entries
            if document is not None
        ]
        written = (
            await mongo.db[collection].bulk_write(operations, ordered=False)
            if operations
            else None
        )
        result.upserted = int(getattr(written, "upserted_count", 0)) if written else 0
        result.matched = int(getattr(written, "matched_count", 0)) if written else 0
    else:
        written = await mongo.write(
            [candle_to_mongo_kline(candle) for _, candle in entries], collection
        )
        result.upserted = int(written)
        result.duplicates = int(getattr(written, "duplicates", 0))

    if result.mysql_copy == "disabled":
        return result
    if mysql is None:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="unavailable").inc()
        logger.error(
            "klines_mysql_copy_unavailable: Mongo write succeeded but no MySQL "
            "adapter is configured; historic completeness is at risk",
            extra={"interval": interval},
        )
        return result

    rows = [candle_to_mysql_kline(candle) for _, candle in entries]
    if wait_for_copy:
        await copy_rows_to_mysql(rows, interval, mysql)
        result.mysql_copy = "copied"
    else:
        _schedule_copy(rows, interval, mysql)
        result.mysql_copy = "scheduled"
    return result


def _as_utc(value: Any) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def kline_rows_from_documents(documents: list[dict[str, Any]], interval: str) -> list:
    """Map extractor-shaped Mongo kline documents to MySQL rows, skipping unmappable ones."""
    rows = []
    for document in documents:
        mapped = map_mongo_kline_doc({**document, "interval": interval})
        if mapped is None:
            continue
        try:
            candle = Candle(
                symbol=document["symbol"],
                timestamp=_as_utc(document["timestamp"]),
                timeframe=interval,
                open=mapped["open"],
                high=mapped["high"],
                low=mapped["low"],
                close=mapped["close"],
                volume=mapped["volume"],
                quote_volume=mapped["quote_volume"],
                trades_count=mapped["trades_count"],
            )
            rows.append(candle_to_mysql_kline(candle))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return rows


def schedule_documents_copy(
    documents: list[dict[str, Any]], interval: str, mysql: Any
) -> str:
    """MySQL copy for klines that a caller already wrote to Mongo as raw documents.

    Used by the generic gateway insert, which stores the caller's documents untouched.
    """
    if not mysql_copy_enabled():
        return "disabled"
    if mysql is None:
        KLINES_MYSQL_COPY.labels(interval=interval, outcome="unavailable").inc()
        logger.error(
            "klines_mysql_copy_unavailable: Mongo write succeeded but no MySQL "
            "adapter is configured; historic completeness is at risk",
            extra={"interval": interval},
        )
        return "unavailable"
    rows = kline_rows_from_documents(documents, interval)
    if len(rows) != len(documents):
        logger.warning(
            "klines_mysql_copy_unmappable interval=%s skipped=%d",
            interval,
            len(documents) - len(rows),
        )
    if rows:
        _schedule_copy(rows, interval, mysql)
    return "scheduled"


def _timestamp(document: dict[str, Any] | None, candle: Candle) -> datetime:
    return document["timestamp"] if document else candle.timestamp
