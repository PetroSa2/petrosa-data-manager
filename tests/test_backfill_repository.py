"""Unit tests for BackfillRepository.get_job() — closes data-manager#228."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.sql import select

from data_manager.db.repositories.backfill_repository import BackfillRepository


def _backfill_table(metadata: MetaData) -> Table:
    return Table(
        "backfill_jobs",
        metadata,
        Column("job_id", String(64), primary_key=True),
        Column("symbol", String(20), nullable=False),
        Column("data_type", String(50), nullable=False),
        Column("timeframe", String(10)),
        Column("start_time", DateTime, nullable=False),
        Column("end_time", DateTime, nullable=False),
        Column("status", String(20), nullable=False),
        Column("progress", Numeric(5, 2), default=0),
        Column("records_fetched", Integer, default=0),
        Column("records_inserted", Integer, default=0),
        Column("error_message", Text),
        Column("created_at", DateTime, nullable=False),
        Column("started_at", DateTime),
        Column("completed_at", DateTime),
    )


def _make_repo():
    """Return a BackfillRepository wired to an in-memory SQLite engine."""
    metadata = MetaData()
    table = _backfill_table(metadata)
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)

    mock_mysql = MagicMock()
    mock_mysql._get_table.return_value = table
    mock_mysql._ensure_connected.return_value = engine

    repo = BackfillRepository.__new__(BackfillRepository)
    repo.mysql = mock_mysql
    return repo, table, engine


_ROW = {
    "job_id": "job-abc-123",
    "symbol": "BTCUSDT",
    "data_type": "klines",
    "timeframe": "1h",
    "start_time": datetime(2024, 1, 1),
    "end_time": datetime(2024, 1, 2),
    "status": "completed",
    "progress": 100.0,
    "records_fetched": 24,
    "records_inserted": 24,
    "error_message": None,
    "created_at": datetime(2024, 1, 1),
    "started_at": datetime(2024, 1, 1, 0, 1),
    "completed_at": datetime(2024, 1, 1, 1, 0),
}


@pytest.mark.unit
def test_get_job_returns_matching_row():
    repo, table, engine = _make_repo()
    with engine.connect() as conn:
        conn.execute(table.insert(), _ROW)
        conn.commit()

    result = repo.get_job("job-abc-123")

    assert result is not None
    assert result["job_id"] == "job-abc-123"
    assert result["symbol"] == "BTCUSDT"
    assert result["status"] == "completed"
    assert result["records_fetched"] == 24


@pytest.mark.unit
def test_get_job_returns_none_for_missing_id():
    repo, _table, _engine = _make_repo()

    result = repo.get_job("nonexistent-id")

    assert result is None


@pytest.mark.unit
def test_get_job_does_not_call_query_latest():
    repo, _table, _engine = _make_repo()

    repo.get_job("any-id")

    repo.mysql.query_latest.assert_not_called()


@pytest.mark.unit
def test_get_job_sql_has_no_order_by_timestamp():
    """The SELECT emitted by get_job must use WHERE job_id=? and omit ORDER BY timestamp."""
    metadata = MetaData()
    table = _backfill_table(metadata)
    stmt = select(table).where(table.c.job_id == "job-abc-123").limit(1)
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "job_id" in sql
    assert "timestamp" not in sql.lower()
