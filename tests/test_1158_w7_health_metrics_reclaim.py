"""Offline and local-MySQL rehearsal tests for EPIC-1158 W7."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager/scripts/migrations"
FORWARD = MIGRATIONS / "010_health_metrics_reclaim.sql"
ROLLBACK = MIGRATIONS / "010_health_metrics_reclaim_rollback.sql"
MYSQL_SKIP_REASON = (
    "local MySQL 5.7 is unavailable; set MYSQL_REHEARSAL_HOST/PORT/USER/PASSWORD "
    "to run the Tier-1 rehearsal"
)


def test_sql_artifacts_are_safe_and_documented() -> None:
    forward = FORWARD.read_text()
    rollback = ROLLBACK.read_text()
    combined = (forward + "\n" + rollback).upper()

    assert "OPTIMIZE TABLE HEALTH_METRICS" in forward.upper()
    assert rollback.strip()
    assert "NO LOGICAL INVERSE" in rollback.upper()
    assert "DBAAS SNAPSHOT" in rollback.upper()
    for forbidden in ("DROP TABLE", "DROP DATABASE", "TRUNCATE"):
        assert forbidden not in combined
    assert "ALTER TABLE KLINES_" not in combined


def test_mysql_skip_reason_is_visible() -> None:
    assert MYSQL_SKIP_REASON
    assert "unavailable" in MYSQL_SKIP_REASON


def test_health_metrics_reclaim_rehearsal() -> None:
    pymysql = pytest.importorskip("pymysql", reason=MYSQL_SKIP_REASON)
    try:
        connection = pymysql.connect(
            host=os.getenv("MYSQL_REHEARSAL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_REHEARSAL_PORT", "3306")),
            user=os.getenv("MYSQL_REHEARSAL_USER", "root"),
            password=os.getenv("MYSQL_REHEARSAL_PASSWORD", "labpass"),
            database=os.getenv("MYSQL_REHEARSAL_DATABASE", "petrosa_lab"),
            connect_timeout=3,
            autocommit=True,
        )
    except Exception as exc:
        pytest.skip(f"{MYSQL_SKIP_REASON}: {exc}")

    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS health_metrics")
            cursor.execute(
                """
                CREATE TABLE health_metrics (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    service VARCHAR(128) NOT NULL,
                    metric VARCHAR(128) NOT NULL,
                    value DOUBLE NOT NULL,
                    timestamp DATETIME NOT NULL,
                    payload TEXT
                ) ENGINE=InnoDB
                """
            )
            cursor.executemany(
                "INSERT INTO health_metrics (service, metric, value, timestamp, payload) "
                "VALUES ('rehearsal', 'load', %s, NOW(), %s)",
                [(float(index), "x" * 2048) for index in range(40)],
            )
            cursor.execute("DELETE FROM health_metrics WHERE id > 2")
            cursor.execute(
                "SELECT data_free FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'health_metrics'"
            )
            before = cursor.fetchone()[0]
            assert before > 0
            cursor.execute("SELECT COUNT(*) FROM health_metrics")
            row_count = cursor.fetchone()[0]
            for statement in FORWARD.read_text().split(";"):
                if statement.strip():
                    cursor.execute(statement)
            cursor.execute(
                "SELECT data_free FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'health_metrics'"
            )
            after = cursor.fetchone()[0]
            assert after < before
            cursor.execute("SELECT COUNT(*) FROM health_metrics")
            assert cursor.fetchone()[0] == row_count
    finally:
        connection.close()
