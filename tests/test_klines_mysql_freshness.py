import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from data_manager.maintenance.klines_mysql_freshness import (
    check_freshness,
    freshness_loop,
)


def _row(timestamp):
    return [{"timestamp": timestamp}]


def test_fresh_copy_sets_fresh_outcome():
    newest = datetime(2026, 10, 6, 17, tzinfo=UTC)
    mongo = SimpleNamespace(query_latest=AsyncMock(return_value=_row(newest)))
    mysql = Mock()
    mysql.query_latest.return_value = _row(newest - timedelta(hours=1))

    result = asyncio.run(check_freshness(mongo, mysql, ["BTCUSDT"], ["1h"]))

    assert result == {"fresh": 1}
    mysql.query_latest.assert_called_once_with("klines_h1", "BTCUSDT", 1)


def test_stale_copy_includes_missing_mysql_rows():
    newest = datetime(2026, 10, 6, 17, tzinfo=UTC)
    mongo = SimpleNamespace(query_latest=AsyncMock(return_value=_row(newest)))
    mysql = Mock()
    mysql.query_latest.return_value = []

    result = asyncio.run(check_freshness(mongo, mysql, ["BTCUSDT"], ["1d"]))

    assert result == {"stale": 1}


def test_failed_pair_does_not_stop_other_checks():
    newest = datetime(2026, 10, 6, 17, tzinfo=UTC)
    mongo = SimpleNamespace(query_latest=AsyncMock(side_effect=[RuntimeError("down"), _row(newest)]))
    mysql = Mock()
    mysql.query_latest.return_value = _row(newest)

    result = asyncio.run(
        check_freshness(mongo, mysql, ["BTCUSDT"], ["1h", "1d"])
    )

    assert result == {"error": 1, "fresh": 1}


def test_freshness_loop_resolves_database_manager_each_cycle():
    stop_event = asyncio.Event()
    manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(query_latest=AsyncMock(return_value=[])),
        mysql_adapter=Mock(),
    )

    async def run_once():
        task = asyncio.create_task(
            freshness_loop(lambda: manager, stop_event, interval_seconds=60)
        )
        await asyncio.sleep(0)
        stop_event.set()
        await task

    asyncio.run(run_once())
    manager.mongodb_adapter.query_latest.assert_called()
