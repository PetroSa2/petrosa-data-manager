"""Read helpers for the CIO keep/kill input: net R per closed round of each strategy (petrosa-data-manager#468).

The ``GET /analysis/strategy-net-r`` route (also under ``/api/v1/analysis``) is what the CIO's keep/kill job reads
(petrosa-cio#299, #306); it is served by ``scorecard.py``, whose router is registered first. This module holds the
read helpers those routes share. Read-only: the rounds come from the round book over ``execution_events``, the stop
a position carried from its ``positions`` row, else from its ``cio_decisions`` row.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

import data_manager.api.app as api_module
from data_manager.services.round_book import FILL_EVENT_TYPES, _when, replay_book
from data_manager.services.strategy_net_r import (
    score_rounds,
    stop_fraction_of,
    strategy_net_r,
)

logger = logging.getLogger(__name__)

_MAX_FILLS = 200_000


def _analytics_database(mongodb: Any) -> Any:
    analytics_db = getattr(mongodb, "__dict__", {}).get("analytics_db")
    return analytics_db if analytics_db is not None else mongodb.db


# petrosa-data-manager#918: the all-time fill read (~3.9k rows with payloads) takes ~4.7 s over the
# DBaaS link, at the 5 s request budget, so the scorecard went `unavailable`. Fills are append-only:
# keep them per process and read only what is new since the watermark (with an overlap for late
# writes), so a request reads seconds of data, not all history.
_FILL_CACHE_OVERLAP = timedelta(minutes=5)
_fill_cache: dict[str, dict[str, Any]] = {}
_fill_cache_watermark: datetime | None = None
_fill_cache_lock = asyncio.Lock()


def _fill_key(row: dict[str, Any]) -> str:
    return f"{row.get('order_id')}:{row.get('event_type')}"


def _fill_time(row: dict[str, Any]) -> datetime:
    from data_manager.services.historic_analytics import _as_datetime

    return _as_datetime(row.get("timestamp")) or datetime.min.replace(tzinfo=UTC)


async def _cached_mysql_fills(mysql: Any) -> list[dict[str, Any]]:
    """All fills, from a process cache topped up with the rows since its watermark."""
    global _fill_cache_watermark
    from data_manager.services.historic_analytics import (
        EXECUTION_EVENT_ANALYTICS_COLUMNS,
        read_execution_events,
    )

    async with _fill_cache_lock:
        if _fill_cache_watermark is None:
            rows, _ = await asyncio.to_thread(read_execution_events, mysql)
        else:
            rows, _ = await asyncio.to_thread(
                read_execution_events,
                mysql,
                start=_fill_cache_watermark - _FILL_CACHE_OVERLAP,
                end=datetime.now(UTC) + _FILL_CACHE_OVERLAP,
            )
        for row in rows:
            # The full read selects EXECUTION_EVENT_ANALYTICS_COLUMNS; the range read returns every
            # column. Keep one row shape in the cache.
            _fill_cache[_fill_key(row)] = {
                column: row.get(column) for column in EXECUTION_EVENT_ANALYTICS_COLUMNS
            }
        if _fill_cache:
            _fill_cache_watermark = max(_fill_time(row) for row in _fill_cache.values())
        return sorted(_fill_cache.values(), key=_fill_time)


def _reset_fill_cache() -> None:
    global _fill_cache_watermark
    _fill_cache.clear()
    _fill_cache_watermark = None


async def _load_fills(end: datetime | None) -> list[dict[str, Any]]:
    from data_manager.services.historic_analytics import get_mysql_adapter

    mysql = get_mysql_adapter(api_module.db_manager)
    if mysql is not None:
        rows = await _cached_mysql_fills(mysql)
        if end is None:
            return list(rows)
        return [row for row in rows if _fill_time(row) < end]
    mongodb = api_module.db_manager.mongodb_adapter
    database = _analytics_database(mongodb)
    query: dict[str, Any] = {"event_type": {"$in": sorted(FILL_EVENT_TYPES)}}
    if end is not None:
        query["timestamp"] = {"$lt": end}
    return (
        await database["execution_events"]
        .find(query)
        .sort("timestamp", 1)
        .to_list(length=_MAX_FILLS)
    )


async def _load_stops(position_ids: list[str]) -> dict[str, Decimal]:
    """position id -> the stop fraction the position carried, from its row."""
    if not position_ids:
        return {}
    from data_manager.services.historic_analytics import get_mysql_adapter

    mysql = get_mysql_adapter(api_module.db_manager)
    if mysql is not None:
        from data_manager.services.historic_analytics import read_table

        rows = await asyncio.to_thread(read_table, mysql, "positions")
    else:
        mongodb = api_module.db_manager.mongodb_adapter
        database = _analytics_database(mongodb)
        rows = await (
            database["positions"]
            .find({"position_id": {"$in": position_ids}})
            .to_list(length=len(position_ids))
        )
    out: dict[str, Decimal] = {}
    for row in rows:
        stop = stop_fraction_of(
            row.get("entry_price") or row.get("avg_price"), row.get("stop_loss")
        )
        if stop is not None and row.get("position_id"):
            out[str(row["position_id"])] = stop
    return out


async def _load_decisions(decision_ids: list[str]) -> dict[str, dict[str, Any]]:
    """decision id -> {"source", "stop_fraction"} from ``cio_decisions``."""
    if not decision_ids:
        return {}
    from data_manager.services.historic_analytics import get_mysql_adapter

    mysql = get_mysql_adapter(api_module.db_manager)
    if mysql is not None:
        from data_manager.services.historic_analytics import read_table

        rows = await asyncio.to_thread(read_table, mysql, "cio_decisions")
    else:
        mongodb = api_module.db_manager.mongodb_adapter
        database = _analytics_database(mongodb)
        rows = await (
            database["cio_decisions"]
            .find({"decision_id": {"$in": decision_ids}})
            .to_list(length=len(decision_ids))
        )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row.get("payload") or {}
        price = payload.get("entry_price") or payload.get("price") or row.get("price")
        stop = stop_fraction_of(price, payload.get("stop_loss"))
        if stop is None and payload.get("stop_loss_pct") is not None:
            try:
                stop = Decimal(str(payload["stop_loss_pct"]))
            except InvalidOperation:
                stop = None
        out[str(row["decision_id"])] = {
            "source": row.get("source"),
            "stop_fraction": stop,
        }
    return out


async def _load_exchange_history() -> dict[str, Any] | None:
    """The exchange snapshot series for the synthetic exits; None when MySQL is unavailable."""
    from data_manager.services.historic_analytics import get_mysql_adapter

    mysql = get_mysql_adapter(api_module.db_manager)
    if mysql is None:
        return None
    from data_manager.db.repositories.ledger_repository import LedgerRepository

    repo = LedgerRepository(
        mysql, getattr(api_module.db_manager, "mongodb_adapter", None)
    )
    return await asyncio.to_thread(repo.round_exchange_history)


async def _scored() -> list:
    from data_manager.services.historic_analytics import get_mysql_adapter

    if not api_module.db_manager or (
        get_mysql_adapter(api_module.db_manager) is None
        and not getattr(api_module.db_manager, "mongodb_adapter", None)
    ):
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    try:
        fills = await _load_fills(None)
        fills.sort(key=lambda row: _when(row) or datetime.min.replace(tzinfo=UTC))
        book = replay_book(fills, exchange_history=await _load_exchange_history())
        position_ids = sorted({p for r in book.closed for p in r.position_ids})
        decision_ids = sorted({d for r in book.closed for d in r.decision_ids})
        stops = await _load_stops(position_ids)
        decisions = await _load_decisions(decision_ids)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("strategy-net-r: read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return score_rounds(book.closed, stops=stops, decisions=decisions)
