"""Read helpers for the CIO keep/kill input: net R per closed round of each strategy (petrosa-data-manager#468).

The ``GET /analysis/strategy-net-r`` route (also under ``/api/v1/analysis``) is what the CIO's keep/kill job reads
(petrosa-cio#299, #306); it is served by ``scorecard.py``, whose router is registered first. This module holds the
read helpers those routes share. Read-only: the rounds come from the round book over ``execution_events``, the stop
a position carried from its ``positions`` row, else from its ``cio_decisions`` row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
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
# DBaaS link, at the 5 s request budget, so the scorecard went `unavailable`. One shared full read
# per TTL fills a process cache; requests are served from it (stale-while-revalidate). The read runs
# as a shared task shielded from request cancellation, so a request that times out still leaves the
# cache filled for the next one; late-written rows are picked up within the TTL.
_FILL_CACHE_TTL_SECONDS = float(os.getenv("SCORECARD_FILL_CACHE_TTL_SECONDS", "300"))
_fill_rows: list[dict[str, Any]] | None = None
_fill_loaded_at = 0.0
_fill_task: asyncio.Task[None] | None = None


def _fill_time(row: dict[str, Any]) -> datetime:
    from data_manager.services.historic_analytics import _as_datetime

    return _as_datetime(row.get("timestamp")) or datetime.min.replace(tzinfo=UTC)


async def _refresh_fills(mysql: Any) -> None:
    global _fill_rows, _fill_loaded_at
    from data_manager.services.historic_analytics import read_execution_events

    rows, _ = await asyncio.to_thread(read_execution_events, mysql)
    _fill_rows = sorted(rows, key=_fill_time)
    _fill_loaded_at = time.monotonic()


def _log_refresh_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("scorecard cache read failed: %s", task.exception())


def _start_refresh(mysql: Any) -> asyncio.Task[None]:
    global _fill_task
    if _fill_task is None or _fill_task.done():
        _fill_task = asyncio.get_running_loop().create_task(_refresh_fills(mysql))
        _fill_task.add_done_callback(_log_refresh_failure)
    return _fill_task


async def _cached_mysql_fills(mysql: Any) -> list[dict[str, Any]]:
    """All fills from the process cache; refreshed at most once per TTL, never inside a request."""
    if _fill_rows is not None:
        if time.monotonic() - _fill_loaded_at >= _FILL_CACHE_TTL_SECONDS:
            _start_refresh(mysql)  # serve the cached rows while the refresh runs
        return _fill_rows
    await asyncio.shield(_start_refresh(mysql))
    return _fill_rows or []


def _reset_fill_cache() -> None:
    global _fill_rows, _fill_loaded_at, _fill_task
    _fill_rows, _fill_loaded_at, _fill_task = None, 0.0, None


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


# petrosa-data-manager#918: the stop and decision lookups read whole tables (positions ~3.3k rows
# 14.4 s, cio_decisions ~14.7k rows 31.6 s over the DBaaS link), so both timed out at the 5 s
# budget and the scorecard stayed `incomplete`. Read only the ids the scored rounds name (both are
# indexed: positions.position_id unique, cio_decisions.decision_id primary key) and only the
# columns the stop math needs, in bounded IN-list chunks. Decision rows never change once written
# and the scored rounds can name thousands of them, so they are cached by id and only unseen ids are
# read, by one shared task shielded from request cancellation (a request that hits the budget still
# leaves the rows cached for the next one, and abandoned reads do not pile up).
_ID_CHUNK = 500
_POSITION_COLUMNS = ("position_id", "entry_price", "avg_price", "stop_loss")
_DECISION_COLUMNS = ("decision_id", "source", "price", "payload")


def _read_rows_by_id(
    mysql: Any, table: str, id_column: str, ids: list[str], columns: tuple[str, ...]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        page, *_ = mysql.find_paginated(
            table,
            filter_dict={id_column: chunk},
            limit=len(chunk),
            columns=columns,
            include_total=False,
        )
        rows.extend(page)
    return rows


_decision_rows: dict[str, dict[str, Any]] = {}
_decision_task: asyncio.Task[None] | None = None


async def _fetch_decision_rows(mysql: Any, ids: list[str]) -> None:
    rows = await asyncio.to_thread(
        _read_rows_by_id, mysql, "cio_decisions", "decision_id", ids, _DECISION_COLUMNS
    )
    for row in rows:
        if row.get("decision_id") is not None:
            _decision_rows[str(row["decision_id"])] = row


async def _cached_decision_rows(mysql: Any, ids: list[str]) -> list[dict[str, Any]]:
    global _decision_task
    if _decision_task is not None and not _decision_task.done():
        await asyncio.shield(_decision_task)
    missing = [i for i in ids if i not in _decision_rows]
    if missing:
        _decision_task = asyncio.get_running_loop().create_task(
            _fetch_decision_rows(mysql, missing)
        )
        _decision_task.add_done_callback(_log_refresh_failure)
        await asyncio.shield(_decision_task)
    return [_decision_rows[i] for i in ids if i in _decision_rows]


def _reset_decision_cache() -> None:
    global _decision_task
    _decision_rows.clear()
    _decision_task = None


async def _load_stops(position_ids: list[str]) -> dict[str, Decimal]:
    """position id -> the stop fraction the position carried, from its row."""
    if not position_ids:
        return {}
    from data_manager.services.historic_analytics import get_mysql_adapter

    mysql = get_mysql_adapter(api_module.db_manager)
    if mysql is not None:
        rows = await asyncio.to_thread(
            _read_rows_by_id,
            mysql,
            "positions",
            "position_id",
            position_ids,
            _POSITION_COLUMNS,
        )
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
        rows = await _cached_decision_rows(mysql, decision_ids)
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
        # MySQL's JSON column arrives as a dict; a Mongo document may carry its payload as text.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
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
