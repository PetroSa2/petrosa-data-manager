"""Daily-candle completeness: which UTC days are missing from MongoDB ``klines_1d`` and MySQL ``klines_d1``.

Volatility, correlation and drawdown inputs need one daily candle per day (petrosa-data-manager#536).
Days are compared as UTC dates over the last ``DAYS`` complete days; today's forming candle is never expected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from prometheus_client import Gauge

import constants
from data_manager.models.health import GapInfo
from data_manager.utils.time_utils import as_aware_utc

logger = logging.getLogger(__name__)

#: How many complete UTC days back the completeness check looks.
DAYS = 90
MONGO_COLLECTION = "klines_1d"
MYSQL_TABLE = "klines_d1"
INTERVAL_SECONDS = 3600

KLINES_1D_MISSING_DAYS = Gauge(
    "data_manager_klines_1d_missing_days",
    "Complete UTC days without a daily candle in the last 90 days",
    ["symbol", "store"],
)
KLINES_1D_COMPLETENESS = Gauge(
    "data_manager_klines_1d_completeness_ratio",
    "Present over expected daily candles in the last 90 complete UTC days",
    ["symbol", "store"],
)


def day_of(value: Any) -> date:
    """The UTC date of a candle timestamp (naive values are UTC, as MySQL returns them)."""
    return as_aware_utc(value).astimezone(UTC).date()


def expected_days(today: date, days: int = DAYS) -> list[date]:
    """The ``days`` complete UTC days before ``today``, oldest first."""
    return [today - timedelta(days=offset) for offset in range(days, 0, -1)]


def missing_days(present: Iterable[date], expected: list[date]) -> list[date]:
    have = set(present)
    return [day for day in expected if day not in have]


def window(today: date, days: int = DAYS) -> tuple[datetime, datetime]:
    """``[start, end)`` of the expected days as UTC datetimes."""
    first = expected_days(today, days)[0]
    return (
        datetime(first.year, first.month, first.day, tzinfo=UTC),
        datetime(today.year, today.month, today.day, tzinfo=UTC),
    )


async def mongo_days(
    mongo: Any, symbol: str, start: datetime, end: datetime
) -> set[date]:
    docs = await mongo.query_range(MONGO_COLLECTION, start, end, symbol)
    return {day_of(doc["timestamp"]) for doc in docs if doc.get("timestamp")}


async def mysql_days(
    mysql: Any, symbol: str, start: datetime, end: datetime
) -> set[date]:
    rows = await asyncio.to_thread(
        mysql.query_range, MYSQL_TABLE, start, end, symbol, columns=("timestamp",)
    )
    return {day_of(row["timestamp"]) for row in rows if row.get("timestamp")}


async def check_daily_completeness(
    mongo: Any,
    mysql: Any,
    symbols: Iterable[str],
    *,
    today: date | None = None,
    days: int = DAYS,
    backfill_gap: Callable[..., Any] | None = None,
) -> dict[tuple[str, str], list[date]]:
    """Missing days per ``(symbol, store)``; also publishes the two gauges. A failed read is skipped."""
    today = today or datetime.now(UTC).date()
    start, end = window(today, days)
    expected = expected_days(today, days)
    report: dict[tuple[str, str], list[date]] = {}
    for symbol in symbols:
        for store, reader, adapter in (
            ("mongodb", mongo_days, mongo),
            ("mysql", mysql_days, mysql),
        ):
            if adapter is None:
                continue
            try:
                present = await reader(adapter, symbol, start, end)
            except Exception:
                logger.warning(
                    "klines_1d_completeness_failed symbol=%s store=%s",
                    symbol,
                    store,
                    exc_info=True,
                )
                continue
            missing = missing_days(present, expected)
            report[(symbol, store)] = missing
            KLINES_1D_MISSING_DAYS.labels(symbol=symbol, store=store).set(len(missing))
            KLINES_1D_COMPLETENESS.labels(symbol=symbol, store=store).set(
                (len(expected) - len(missing)) / len(expected)
            )
            if (
                store == "mongodb"
                and missing
                and constants.ENABLE_AUTO_BACKFILL
                and backfill_gap is not None
            ):
                for day in missing:
                    start_day = datetime(day.year, day.month, day.day, tzinfo=UTC)
                    await backfill_gap(
                        symbol,
                        "1d",
                        GapInfo(
                            start_time=start_day,
                            end_time=start_day + timedelta(days=1),
                            duration_seconds=86400,
                            expected_records=1,
                        ),
                        "high",
                    )
            if missing:
                logger.warning(
                    "klines_1d_gap symbol=%s store=%s missing=%d first=%s last=%s",
                    symbol,
                    store,
                    len(missing),
                    missing[0],
                    missing[-1],
                )
    return report


async def daily_completeness_loop(
    db_manager_source: Any,
    stop_event: asyncio.Event,
    *,
    interval_seconds: int = INTERVAL_SECONDS,
    is_leader: Callable[[], bool] | None = None,
    backfill_gap: Callable[..., Any] | None = None,
) -> None:
    """Run the check hourly, on the leader replica only, until the application stops."""
    while not stop_event.is_set():
        if is_leader is None or is_leader():
            manager = (
                db_manager_source()
                if callable(db_manager_source)
                else db_manager_source
            )
            mongo = getattr(manager, "mongodb_adapter", None)
            mysql = getattr(manager, "mysql_adapter", None)
            if mongo is not None or mysql is not None:
                await check_daily_completeness(
                    mongo,
                    mysql,
                    constants.SUPPORTED_PAIRS,
                    backfill_gap=backfill_gap,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
