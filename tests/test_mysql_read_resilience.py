from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

import constants
from data_manager.api.middleware import metrics
from data_manager.auditor.scheduler import AuditScheduler
from data_manager.db import mysql_adapter as mysql_adapter_module
from data_manager.db.mysql_adapter import MySQLAdapter


def _adapter_with_fake_connection() -> tuple[MySQLAdapter, MagicMock]:
    adapter = MySQLAdapter("sqlite:///:memory:")
    table = sa.Table(
        "klines_h1",
        adapter.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(20)),
        sa.Column("timestamp", sa.DateTime),
    )
    adapter.tables["klines_h1"] = table
    adapter._connected = True
    connection = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    adapter.engine = engine
    return adapter, connection


def _lost_connection() -> OperationalError:
    return OperationalError("SELECT", {}, Exception(2013, "Lost connection"))


@pytest.fixture
def no_wait_retry(monkeypatch):
    real_retry = mysql_adapter_module.retry_transient

    def retry_without_sleep(func, **kwargs):
        return real_retry(func, sleeper=lambda _delay: None, **kwargs)

    monkeypatch.setattr(mysql_adapter_module, "retry_transient", retry_without_sleep)
    monkeypatch.setattr(constants, "DB_RECONNECT_MAX_ATTEMPTS", 3)


def test_query_range_retries_transient_2013_then_succeeds(no_wait_retry):
    adapter, connection = _adapter_with_fake_connection()
    connection.execute.side_effect = [
        _lost_connection(),
        [SimpleNamespace(_mapping={"symbol": "BTCUSDT"})],
    ]

    rows = adapter.query_range(
        "klines_h1",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        "BTCUSDT",
    )

    assert rows == [{"symbol": "BTCUSDT"}]
    assert connection.execute.call_count == 2
    assert adapter.circuit_breaker.failure_count == 0


def test_query_range_raises_after_retries_and_counts_one_breaker_failure(
    no_wait_retry,
):
    adapter, connection = _adapter_with_fake_connection()
    connection.execute.side_effect = _lost_connection()

    with pytest.raises(Exception, match="Failed to query range"):
        adapter.query_range(
            "klines_h1",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "BTCUSDT",
        )

    assert connection.execute.call_count == 3
    assert adapter.circuit_breaker.failure_count == 1
    assert (
        metrics.MYSQL_READ_FAILURES.labels(
            database="mysql", collection="klines_h1", reason="database_error"
        )._value.get()
        >= 1
    )


def test_non_transient_read_error_is_not_retried(no_wait_retry):
    adapter, connection = _adapter_with_fake_connection()
    connection.execute.side_effect = RuntimeError("bad SQL")

    with pytest.raises(Exception, match="Failed to query range"):
        adapter.query_range(
            "klines_h1",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "BTCUSDT",
        )

    assert connection.execute.call_count == 1


@pytest.mark.asyncio
async def test_audit_cycle_isolates_exhausted_read_per_symbol(monkeypatch):
    scheduler = object.__new__(AuditScheduler)
    scheduler.backfill_queue = None
    scheduler.last_audit_time = None
    scheduler.gap_detector = SimpleNamespace(
        detect_gaps=AsyncMock(side_effect=[RuntimeError("read retries exhausted"), []])
    )
    scheduler.duplicate_detector = SimpleNamespace(
        detect_duplicates=AsyncMock(return_value=0)
    )
    scheduler.health_scorer = SimpleNamespace(
        calculate_health=AsyncMock(return_value=SimpleNamespace(quality_score=100))
    )
    scheduler.evaluator = SimpleNamespace(tick_with_sample=AsyncMock())
    monkeypatch.setattr(constants, "SUPPORTED_PAIRS", ["FAIL", "OK"])
    monkeypatch.setattr(constants, "SUPPORTED_TIMEFRAMES", ["1h"])
    monkeypatch.setattr(constants, "MAX_CONCURRENT_TASKS", 2)

    await scheduler.run_audit_cycle()

    assert scheduler.last_audit_time is not None
    scheduler.evaluator.tick_with_sample.assert_awaited_once()
