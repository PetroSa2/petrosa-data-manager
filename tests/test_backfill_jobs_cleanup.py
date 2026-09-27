from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, select
from sqlalchemy.pool import StaticPool

from data_manager.maintenance.backfill_jobs_cleanup import (
    LEGACY_ERROR,
    cleanup_legacy_jobs,
)


def _fixture():
    metadata = MetaData()
    table = Table(
        "backfill_jobs",
        metadata,
        Column("job_id", String(64), primary_key=True),
        Column("data_type", String(50), nullable=False),
        Column("status", String(20), nullable=False),
        Column("error_message", String(255)),
        Column("completed_at", DateTime),
    )
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            table.insert(),
            [
                {"job_id": "legacy", "data_type": "", "status": "pending"},
                {"job_id": "valid", "data_type": "candles", "status": "pending"},
            ],
        )
    mysql = MagicMock()
    mysql._get_table.return_value = table
    mysql._ensure_connected.return_value = engine
    return mysql, table, engine


@pytest.mark.unit
def test_cleanup_dry_run_does_not_update():
    mysql, table, engine = _fixture()

    assert cleanup_legacy_jobs(mysql) == 1
    with engine.connect() as conn:
        rows = conn.execute(select(table).order_by(table.c.job_id)).all()
    assert rows[0].status == "pending"
    assert rows[0].error_message is None


@pytest.mark.unit
def test_cleanup_apply_updates_only_empty_data_type_rows():
    mysql, table, engine = _fixture()

    assert cleanup_legacy_jobs(mysql, dry_run=False) == 1
    with engine.connect() as conn:
        rows = conn.execute(select(table).order_by(table.c.job_id)).all()
    assert rows[0].status == "failed"
    assert rows[0].error_message == LEGACY_ERROR
    assert rows[0].completed_at is not None
    assert rows[1].status == "pending"
    assert rows[1].error_message is None
