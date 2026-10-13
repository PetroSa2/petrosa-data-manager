"""Prometheus completeness metric for the permanent MySQL kline archive."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge

import constants
from data_manager.db.repositories.candle_repository import mysql_table_name
from data_manager.utils.time_utils import parse_timeframe_to_minutes

logger = logging.getLogger(__name__)

MYSQL_KLINES_COMPLETENESS = Gauge(
    "data_manager_mysql_klines_completeness_ratio",
    "Expected versus present historic MySQL klines",
    ["symbol", "timeframe"],
)
MYSQL_KLINES_COMPLETENESS_CHECKS = Counter(
    "data_manager_mysql_klines_completeness_checks_total",
    "MySQL kline completeness check outcomes",
    ["symbol", "timeframe", "outcome"],
)
MYSQL_KLINES_COMPLETENESS_LAST_RUN = Gauge(
    "data_manager_mysql_klines_completeness_last_run_timestamp",
    "Unix timestamp of the last MySQL kline completeness sweep",
)


def initialize_completeness_metrics() -> None:
    """Retained for callers that explicitly initialize metrics.

    Samples are intentionally not created until a pair has been computed, so
    an unrun process cannot export a misleading zero.
    """


def completeness_ratio(
    present: int, start: datetime, end: datetime, timeframe: str
) -> float:
    """Return the bounded [0, 1] ratio for a regular candle series."""
    minutes = parse_timeframe_to_minutes(timeframe)
    expected = max(0, int((end - start).total_seconds() // (minutes * 60)))
    return min(1.0, present / expected) if expected else 1.0


def update_completeness(
    mysql: Any, symbol: str, timeframe: str, start: datetime, end: datetime
) -> float:
    """Calculate and publish one symbol/timeframe archive ratio."""
    present = int(
        mysql.get_record_count(mysql_table_name(timeframe), start, end, symbol)
    )
    ratio = completeness_ratio(present, start, end, timeframe)
    MYSQL_KLINES_COMPLETENESS.labels(symbol=symbol, timeframe=timeframe).set(ratio)
    return ratio


async def completeness_loop(
    db_manager_source: Any,
    stop_event: asyncio.Event,
    *,
    interval_seconds: int | None = None,
    lookback_days: int | None = None,
    is_leader: Callable[[], bool] | None = None,
    now: Callable[[], datetime] | None = None,
    symbols: Iterable[str] | None = None,
    timeframes: Iterable[str] | None = None,
) -> None:
    """Refresh archive completeness until shutdown, on the current leader only."""
    delay = interval_seconds or constants.KLINES_MYSQL_COMPLETENESS_INTERVAL_SECONDS
    lookback = lookback_days or constants.KLINES_MYSQL_COMPLETENESS_LOOKBACK_DAYS
    clock = now or (lambda: datetime.now(UTC))
    pairs = symbols if symbols is not None else constants.SUPPORTED_PAIRS
    periods = timeframes if timeframes is not None else constants.SUPPORTED_TIMEFRAMES

    while not stop_event.is_set():
        db_manager = (
            db_manager_source() if callable(db_manager_source) else db_manager_source
        )
        mysql = getattr(db_manager, "mysql_adapter", None)
        if (is_leader is None or is_leader()) and mysql is not None:
            end = clock()
            start = end - timedelta(days=lookback)
            for symbol in pairs:
                for timeframe in periods:
                    try:
                        await asyncio.to_thread(
                            update_completeness,
                            mysql,
                            symbol,
                            timeframe,
                            start,
                            end,
                        )
                        MYSQL_KLINES_COMPLETENESS_CHECKS.labels(
                            symbol=symbol, timeframe=timeframe, outcome="success"
                        ).inc()
                    except Exception:
                        MYSQL_KLINES_COMPLETENESS_CHECKS.labels(
                            symbol=symbol, timeframe=timeframe, outcome="error"
                        ).inc()
                        logger.warning(
                            "mysql_klines_completeness_check_failed symbol=%s timeframe=%s",
                            symbol,
                            timeframe,
                            exc_info=True,
                        )
            MYSQL_KLINES_COMPLETENESS_LAST_RUN.set(end.timestamp())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            continue
