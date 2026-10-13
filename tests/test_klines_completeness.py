import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from data_manager.maintenance import klines_completeness as completeness

INITIAL_COMPLETENESS_SAMPLES = completeness.MYSQL_KLINES_COMPLETENESS.collect()[
    0
].samples


class RecordingMysql:
    def __init__(self, counts=None, failing=None):
        self.counts = counts or {}
        self.failing = failing
        self.threads = []

    def get_record_count(self, table, start, end, symbol):
        self.threads.append(threading.get_ident())
        if symbol == self.failing:
            raise RuntimeError("count failed")
        return self.counts.get((table, symbol), 1)


def _run_loop(mysql, *, leader=True, symbols=("BTCUSDT",)):
    stop = asyncio.Event()
    main_thread = threading.get_ident()
    manager = SimpleNamespace(mysql_adapter=mysql)
    fixed_now = datetime(2026, 10, 13, tzinfo=UTC)

    async def run():
        task = asyncio.create_task(
            completeness.completeness_loop(
                lambda: manager,
                stop,
                interval_seconds=60,
                lookback_days=1,
                is_leader=lambda: leader,
                now=lambda: fixed_now,
                symbols=symbols,
                timeframes=("1h", "1d"),
            )
        )
        await asyncio.sleep(0)
        stop.set()
        await task

    asyncio.run(run())
    return main_thread


def test_loop_sets_ratio_and_last_run_timestamp():
    mysql = RecordingMysql({("klines_h1", "BTCUSDT"): 24, ("klines_d1", "BTCUSDT"): 1})

    _run_loop(mysql)

    assert completeness.MYSQL_KLINES_COMPLETENESS.labels(
        symbol="BTCUSDT", timeframe="1h"
    )._value.get() == pytest.approx(1.0)
    assert (
        completeness.MYSQL_KLINES_COMPLETENESS_LAST_RUN._value.get()
        == pytest.approx(datetime(2026, 10, 13, tzinfo=UTC).timestamp())
    )


def test_non_leader_skips_and_worker_thread_is_used():
    mysql = RecordingMysql()
    _run_loop(mysql, leader=False)
    assert mysql.threads == []

    mysql = RecordingMysql()
    main_thread = _run_loop(mysql, leader=True)
    assert mysql.threads
    assert all(thread_id != main_thread for thread_id in mysql.threads)


def test_pair_errors_are_isolated_and_counted():
    mysql = RecordingMysql(failing="BTCUSDT")

    _run_loop(mysql, symbols=("BTCUSDT", "ETHUSDT"))

    assert len(mysql.threads) == 4
    assert completeness.MYSQL_KLINES_COMPLETENESS.labels(
        symbol="ETHUSDT", timeframe="1h"
    )._value.get() == pytest.approx(1 / 24)
    assert (
        completeness.MYSQL_KLINES_COMPLETENESS_CHECKS.labels(
            symbol="BTCUSDT", timeframe="1h", outcome="error"
        )._value.get()
        >= 1
    )


def test_import_does_not_export_zero_sample():
    assert INITIAL_COMPLETENESS_SAMPLES == []
