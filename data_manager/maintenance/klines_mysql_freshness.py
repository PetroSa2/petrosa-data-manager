"""Monitor freshness of the durable MySQL kline copy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge

import constants
from data_manager.db.repositories.candle_repository import mysql_table_name
from data_manager.utils.time_utils import parse_timeframe_to_seconds

logger = logging.getLogger(__name__)

KLINES_MYSQL_LAG_SECONDS = Gauge(
    "data_manager_klines_mysql_lag_seconds",
    "Seconds by which the MySQL kline copy trails MongoDB",
    ["symbol", "interval"],
)
KLINES_MYSQL_STALE = Gauge(
    "data_manager_klines_mysql_stale",
    "Whether the MySQL kline copy trails MongoDB by more than two intervals",
    ["symbol", "interval"],
)
KLINES_MYSQL_FRESHNESS_CHECKS = Counter(
    "data_manager_klines_mysql_freshness_checks_total",
    "Kline MySQL freshness check outcomes",
    ["interval", "outcome"],
)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("invalid kline timestamp")
    return result if result.tzinfo else result.replace(tzinfo=UTC)


async def check_freshness(
    mongo: Any,
    mysql: Any,
    symbols: Iterable[str],
    intervals: Iterable[str],
) -> dict[str, int]:
    """Compare newest Mongo and MySQL timestamps and update freshness metrics."""
    outcomes: dict[str, int] = {}
    for symbol in symbols:
        for interval in intervals:
            try:
                mongo_rows = await mongo.query_latest(f"klines_{interval}", symbol, 1)
                if not mongo_rows:
                    KLINES_MYSQL_FRESHNESS_CHECKS.labels(
                        interval=interval, outcome="no_mongo_data"
                    ).inc()
                    continue
                mysql_rows = await asyncio.to_thread(
                    mysql.query_latest, mysql_table_name(interval), symbol, 1
                )
                mongo_time = _timestamp(mongo_rows[0]["timestamp"])
                mysql_time = (
                    _timestamp(mysql_rows[0]["timestamp"]) if mysql_rows else None
                )
                lag = (
                    max(0.0, (mongo_time - mysql_time).total_seconds())
                    if mysql_time
                    else float("inf")
                )
                stale = not mysql_time or lag > 2 * parse_timeframe_to_seconds(interval)
                KLINES_MYSQL_LAG_SECONDS.labels(symbol=symbol, interval=interval).set(
                    lag if mysql_time else -1
                )
                KLINES_MYSQL_STALE.labels(symbol=symbol, interval=interval).set(
                    int(stale)
                )
                outcome = "stale" if stale else "fresh"
                KLINES_MYSQL_FRESHNESS_CHECKS.labels(
                    interval=interval, outcome=outcome
                ).inc()
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                if stale:
                    logger.warning(
                        "mysql_kline_copy_stale symbol=%s interval=%s lag_seconds=%s",
                        symbol,
                        interval,
                        "missing" if not mysql_time else round(lag, 1),
                    )
            except Exception:
                KLINES_MYSQL_FRESHNESS_CHECKS.labels(
                    interval=interval, outcome="error"
                ).inc()
                KLINES_MYSQL_STALE.labels(symbol=symbol, interval=interval).set(1)
                logger.warning(
                    "mysql_kline_freshness_check_failed symbol=%s interval=%s",
                    symbol,
                    interval,
                    exc_info=True,
                )
                outcomes["error"] = outcomes.get("error", 0) + 1
    return outcomes


async def freshness_loop(
    db_manager_source: Any,
    stop_event: asyncio.Event,
    *,
    interval_seconds: int | None = None,
    is_leader: Callable[[], bool] | None = None,
) -> None:
    """Run freshness checks until the application asks the loop to stop.

    With ``is_leader`` the checks run only while this replica holds the lease, so replicas do not
    repeat each other.
    """
    delay = interval_seconds or constants.KLINES_MYSQL_FRESHNESS_INTERVAL_SECONDS
    while not stop_event.is_set():
        db_manager = (
            db_manager_source() if callable(db_manager_source) else db_manager_source
        )
        mongo = getattr(db_manager, "mongodb_adapter", None)
        mysql = getattr(db_manager, "mysql_adapter", None)
        if (
            (is_leader is None or is_leader())
            and mongo is not None
            and mysql is not None
        ):
            await check_freshness(
                mongo,
                mysql,
                constants.SUPPORTED_PAIRS,
                constants.SUPPORTED_INTERVALS,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            continue
