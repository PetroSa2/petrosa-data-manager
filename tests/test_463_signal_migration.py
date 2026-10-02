"""MySQL 5.7 contract for the durable signal schema migration."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "016_signals_point_in_time.sql"
ROLLBACK = MIGRATIONS / "016_signals_point_in_time_rollback.sql"


def test_signal_migration_is_additive_and_has_rollback():
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    forward = FORWARD.read_text().upper()
    assert "ADD COLUMN IF NOT EXISTS" not in forward
    assert "INFORMATION_SCHEMA.COLUMNS" in forward
    assert "CREATE UNIQUE INDEX UQ_SIGNALS_SIGNAL_KEY" in forward
    assert "DROP TABLE" not in forward
    assert "TRUNCATE" not in forward
    assert "DELETE " not in forward


def test_signal_rollback_targets_only_added_schema():
    rollback = ROLLBACK.read_text().upper()
    assert "DROP INDEX UQ_SIGNALS_SIGNAL_KEY" in rollback
    assert "DROP COLUMN SIGNAL_KEY" in rollback
    assert "INFORMATION_SCHEMA.COLUMNS" in rollback
    assert "DROP TABLE" not in rollback
    assert "DELETE FROM" not in rollback


def test_signal_migration_rehearsal_on_mysql_57():
    pymysql = pytest.importorskip("pymysql")
    container_name = "petrosa-signal-migration-mysql-57"
    external_host = os.getenv("MYSQL_REHEARSAL_HOST")
    owns_container = external_host is None
    port = int(os.getenv("MYSQL_REHEARSAL_PORT", "3306"))
    if owns_container:
        subprocess.run(
            ["docker", "rm", "-f", container_name], check=False, capture_output=True
        )
        docker = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                container_name,
                "-e",
                "MYSQL_ROOT_PASSWORD=labpass",
                "-e",
                "MYSQL_DATABASE=petrosa_lab",
                "-p",
                "0:3306",
                "mysql:5.7",
            ],
            capture_output=True,
            text=True,
        )
        if docker.returncode != 0:
            pytest.skip("Docker is unavailable for the local MySQL 5.7 rehearsal")
        port_result = subprocess.run(
            ["docker", "port", container_name, "3306/tcp"],
            check=True,
            capture_output=True,
            text=True,
        )
        port = int(port_result.stdout.splitlines()[0].rsplit(":", 1)[1].strip())

    try:
        connection = None
        for _ in range(30):
            try:
                connection = pymysql.connect(
                    host=external_host or "127.0.0.1",
                    port=port,
                    user=os.getenv("MYSQL_REHEARSAL_USER", "root"),
                    password=os.getenv("MYSQL_REHEARSAL_PASSWORD", "labpass"),
                    database=os.getenv("MYSQL_REHEARSAL_DATABASE", "petrosa_lab"),
                    connect_timeout=2,
                    autocommit=True,
                )
                break
            except pymysql.MySQLError:
                time.sleep(2)
        if connection is None:
            pytest.fail("MySQL 5.7 container did not become ready")

        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE signals (id BIGINT PRIMARY KEY, symbol VARCHAR(32), timeframe VARCHAR(16), "
                    "period INT, signal_type VARCHAR(64), confidence DECIMAL(10, 8), strategy VARCHAR(128), "
                    "metadata JSON, timestamp DATETIME, created_at DATETIME)"
                )
                base_columns = {
                    "id",
                    "symbol",
                    "timeframe",
                    "period",
                    "signal_type",
                    "confidence",
                    "strategy",
                    "metadata",
                    "timestamp",
                    "created_at",
                }
                added_columns = {
                    "signal_key",
                    "bar_open_time",
                    "bar_close_time",
                    "entry_ref_price",
                    "stop_loss",
                    "take_profit",
                    "decision_id",
                    "signal_revision_payload_hash",
                    "last_rejected_payload_hash",
                    "signal_revision_conflicts",
                }
                for migration in (FORWARD, FORWARD, ROLLBACK, ROLLBACK):
                    for statement in migration.read_text().split(";"):
                        if statement.strip():
                            cursor.execute(statement)
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'signals'"
                )
                columns = {row[0] for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = 'signals' "
                    "AND index_name = 'uq_signals_signal_key'"
                )
                index_count = cursor.fetchone()[0]
                if migration == FORWARD:
                    assert columns == base_columns | added_columns
                    assert index_count == 1
                else:
                    assert columns == base_columns
                    assert index_count == 0
    finally:
        if owns_container:
            subprocess.run(
                ["docker", "rm", "-f", container_name], check=False, capture_output=True
            )
