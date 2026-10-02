"""Static checks and optional local rehearsal for data-manager#467 migration SQL."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD_014 = MIGRATIONS / "014_ledger_exchange_store.sql"
FORWARD = MIGRATIONS / "015_ledger_adjustments_audit.sql"
ROLLBACK = MIGRATIONS / "015_ledger_adjustments_audit_rollback.sql"
MYSQL_SKIP_REASON = (
    "local MySQL rehearsal is unavailable; set MYSQL_REHEARSAL_HOST/PORT/USER/PASSWORD"
)


def test_migration_files_exist_and_are_non_destructive():
    assert FORWARD_014.is_file()
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    forward = FORWARD.read_text().upper()
    rollback = ROLLBACK.read_text().upper()
    assert "CREATE TABLE IF NOT EXISTS LEDGER_ADJUSTMENTS" in forward
    assert "CREATE TABLE IF NOT EXISTS LEDGER_ADJUSTMENTS" in rollback
    for forbidden in ("DROP ", "TRUNCATE ", "DELETE "):
        assert forbidden not in forward
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in rollback


def test_migration_is_explicitly_idempotent():
    forward = FORWARD.read_text().upper()
    assert "CREATE TABLE IF NOT EXISTS" in forward
    assert "INFORMATION_SCHEMA.COLUMNS" in forward
    assert "ADD COLUMN IF NOT EXISTS" not in forward
    assert "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in forward
    assert "015_LEDGER_ADJUSTMENTS_AUDIT" in forward


def test_ledger_store_migration_is_tracked():
    forward = FORWARD_014.read_text().upper()
    assert "CREATE TABLE IF NOT EXISTS LEDGER_EXCHANGE_DAY_REVISION" in forward
    assert "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in forward
    assert "014_LEDGER_EXCHANGE_STORE" in forward


def test_migration_rehearsal_apply_rollback_apply():
    pymysql = pytest.importorskip("pymysql", reason=MYSQL_SKIP_REASON)
    try:
        connection = pymysql.connect(
            host=os.getenv("MYSQL_REHEARSAL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_REHEARSAL_PORT", "3306")),
            user=os.getenv("MYSQL_REHEARSAL_USER", "root"),
            passwd=os.getenv("MYSQL_REHEARSAL_PASSWORD", "labpass"),
            database=os.getenv("MYSQL_REHEARSAL_DATABASE", "petrosa_lab"),
            connect_timeout=3,
            autocommit=True,
        )
    except Exception as exc:  # pragma: no cover - depends on local DB availability
        pytest.skip(f"{MYSQL_SKIP_REASON}: {exc}")

    try:
        with connection.cursor() as cursor:
            for sql in (
                FORWARD_014.read_text(),
                FORWARD_014.read_text(),
                FORWARD.read_text(),
                FORWARD.read_text(),
                ROLLBACK.read_text(),
                FORWARD.read_text(),
            ):
                for statement in sql.split(";"):
                    if statement.strip():
                        cursor.execute(statement)
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'ledger_adjustments'"
            )
            assert cursor.fetchone()[0] == 1
    finally:
        connection.close()
