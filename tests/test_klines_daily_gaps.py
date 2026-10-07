"""Daily-candle gaps in MongoDB klines_1d and MySQL klines_d1 (petrosa-data-manager#536)."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.maintenance.klines_daily_gaps import (
    KLINES_1D_COMPLETENESS,
    KLINES_1D_MISSING_DAYS,
    check_daily_completeness,
    daily_completeness_loop,
    day_of,
    expected_days,
    missing_days,
    window,
)

TODAY = date(2026, 10, 7)


def _stamp(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _days(first: date, count: int, skip=()):
    return [
        first + timedelta(days=i)
        for i in range(count)
        if (first + timedelta(days=i)) not in skip
    ]


def test_the_expected_days_are_the_complete_days_before_today():
    days = expected_days(TODAY, 90)

    assert len(days) == 90
    assert days[-1] == date(
        2026, 10, 6
    )  # yesterday: today's forming candle is not expected
    assert days[0] == date(2026, 7, 9)
    assert TODAY not in days


def test_the_window_covers_exactly_the_expected_days():
    start, end = window(TODAY, 90)

    assert start == _stamp(date(2026, 7, 9))
    assert end == _stamp(TODAY)


def test_day_of_reads_utc_dates_from_aware_naive_and_text_timestamps():
    assert day_of(datetime(2026, 10, 6, 23, 59, tzinfo=UTC)) == date(2026, 10, 6)
    assert day_of(datetime(2026, 10, 6)) == date(2026, 10, 6)  # MySQL: naive means UTC
    assert day_of("2026-10-06T00:00:00Z") == date(2026, 10, 6)


def test_missing_days_are_the_expected_ones_not_present():
    expected = expected_days(TODAY, 5)  # Oct 2..6

    assert missing_days({date(2026, 10, 2), date(2026, 10, 4)}, expected) == [
        date(2026, 10, 3),
        date(2026, 10, 5),
        date(2026, 10, 6),
    ]


def _stores(mongo_missing=(), mysql_missing=()):
    first = date(2026, 7, 9)
    mongo = SimpleNamespace(
        query_range=AsyncMock(
            return_value=[
                {"timestamp": _stamp(d)} for d in _days(first, 90, mongo_missing)
            ]
        )
    )
    mysql = Mock()
    mysql.query_range.return_value = [
        {"timestamp": datetime(d.year, d.month, d.day)}
        for d in _days(first, 90, mysql_missing)
    ]
    return mongo, mysql


@pytest.mark.asyncio
async def test_a_sparse_mongo_collection_is_reported_with_its_missing_days_and_gauges():
    gaps = {date(2026, 8, 1), date(2026, 9, 15)}
    mongo, mysql = _stores(mongo_missing=gaps)

    report = await check_daily_completeness(mongo, mysql, ["BTCUSDT"], today=TODAY)

    assert report[("BTCUSDT", "mongodb")] == sorted(gaps)
    assert report[("BTCUSDT", "mysql")] == []
    assert (
        KLINES_1D_MISSING_DAYS.labels(symbol="BTCUSDT", store="mongodb")._value.get()
        == 2
    )
    assert (
        KLINES_1D_MISSING_DAYS.labels(symbol="BTCUSDT", store="mysql")._value.get() == 0
    )
    assert KLINES_1D_COMPLETENESS.labels(
        symbol="BTCUSDT", store="mongodb"
    )._value.get() == pytest.approx(88 / 90)


@pytest.mark.asyncio
async def test_a_complete_series_reports_no_gaps():
    mongo, mysql = _stores()

    report = await check_daily_completeness(mongo, mysql, ["ETHUSDT"], today=TODAY)

    assert report == {("ETHUSDT", "mongodb"): [], ("ETHUSDT", "mysql"): []}
    assert (
        KLINES_1D_COMPLETENESS.labels(symbol="ETHUSDT", store="mongodb")._value.get()
        == 1.0
    )


@pytest.mark.asyncio
async def test_a_failing_store_is_skipped_and_the_other_still_reported():
    mongo = SimpleNamespace(query_range=AsyncMock(side_effect=RuntimeError("down")))
    _, mysql = _stores()

    report = await check_daily_completeness(mongo, mysql, ["BCHUSDT"], today=TODAY)

    assert ("BCHUSDT", "mongodb") not in report
    assert report[("BCHUSDT", "mysql")] == []


@pytest.mark.asyncio
async def test_the_loop_checks_on_the_leader_only():
    mongo, mysql = _stores()
    manager = SimpleNamespace(mongodb_adapter=mongo, mysql_adapter=mysql)

    async def run(leader):
        stop = asyncio.Event()
        task = asyncio.create_task(
            daily_completeness_loop(
                lambda: manager, stop, interval_seconds=60, is_leader=lambda: leader
            )
        )
        await asyncio.sleep(0.01)
        stop.set()
        await task

    await run(False)
    mongo.query_range.assert_not_called()
    await run(True)
    mongo.query_range.assert_called()
