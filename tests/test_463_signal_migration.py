"""MySQL 5.7 contract for the durable signal schema migration."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "016_signals_point_in_time.sql"
ROLLBACK = MIGRATIONS / "016_signals_point_in_time_rollback.sql"

BASE_COLUMNS = {
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
ADDED_COLUMNS = {
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


def _code(path: Path) -> str:
    """The file without its comment lines."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def _statements(path: Path) -> list[str]:
    return [part.strip() for part in _code(path).split(";") if part.strip()]


def test_signal_migration_is_additive_and_has_rollback():
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    forward = FORWARD.read_text().upper()
    assert "ADD COLUMN IF NOT EXISTS" not in forward
    assert "INFORMATION_SCHEMA.COLUMNS" in forward
    assert "ADD UNIQUE INDEX UQ_SIGNALS_SIGNAL_KEY" in forward
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


@pytest.mark.parametrize("path", [FORWARD, ROLLBACK], ids=["forward", "rollback"])
def test_each_file_builds_exactly_one_online_alter(path: Path):
    """On MySQL 5.7 each ADD or DROP COLUMN rebuilds the table, so everything goes into one ALTER."""
    code = _code(path).upper()
    assert code.count("ALTER TABLE") == 1
    assert len(re.findall(r"^PREPARE ", code, re.M)) == 1
    assert "ALGORITHM=INPLACE, LOCK=NONE" in code


def _rows(cursor, sql: str) -> set[str]:
    cursor.execute(sql)
    return {row[0] for row in cursor.fetchall()}


def _columns(cursor) -> set[str]:
    return _rows(
        cursor,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'signals'",
    )


def _has_index(cursor) -> bool:
    return bool(
        _rows(
            cursor,
            "SELECT index_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'signals' "
            "AND index_name = 'uq_signals_signal_key'",
        )
    )


def _alters(cursor) -> int:
    cursor.execute("SHOW GLOBAL STATUS LIKE 'Com_alter_table'")
    return int(cursor.fetchone()[1])


def _run(cursor, path: Path) -> int:
    """Run a migration file and return how many ALTER TABLE statements the server executed."""
    before = _alters(cursor)
    for statement in _statements(path):
        cursor.execute(statement)
    return _alters(cursor) - before


def _fresh_table(cursor) -> None:
    cursor.execute("DROP TABLE IF EXISTS signals")
    cursor.execute(
        "CREATE TABLE signals (id BIGINT PRIMARY KEY, symbol VARCHAR(32), timeframe VARCHAR(16), "
        "period INT, signal_type VARCHAR(64), confidence DECIMAL(10, 8), strategy VARCHAR(128), "
        "metadata JSON, timestamp DATETIME, created_at DATETIME)"
    )


def _connect(pymysql):
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
    return connection, owns_container, container_name


def test_signal_migration_rehearsal_on_mysql_57():
    pymysql = pytest.importorskip("pymysql")
    connection, owns_container, container_name = _connect(pymysql)
    try:
        if connection is None:
            pytest.fail("MySQL 5.7 container did not become ready")
        with connection, connection.cursor() as cursor:
            # From the base table: one ALTER adds all ten columns and the index, a rerun runs none.
            _fresh_table(cursor)
            assert _run(cursor, FORWARD) == 1
            assert _columns(cursor) == BASE_COLUMNS | ADDED_COLUMNS and _has_index(
                cursor
            )
            assert _run(cursor, FORWARD) == 0
            assert _columns(cursor) == BASE_COLUMNS | ADDED_COLUMNS and _has_index(
                cursor
            )

            # The rollback is one ALTER too, and a rerun runs none.
            assert _run(cursor, ROLLBACK) == 1
            assert _columns(cursor) == BASE_COLUMNS and not _has_index(cursor)
            assert _run(cursor, ROLLBACK) == 0
            assert _columns(cursor) == BASE_COLUMNS and not _has_index(cursor)

            # Partly applied (signal_key exists, as after an interrupted run): one ALTER adds only the rest.
            _fresh_table(cursor)
            cursor.execute(
                "ALTER TABLE signals ADD COLUMN signal_key VARCHAR(191) NULL"
            )
            assert _run(cursor, FORWARD) == 1
            assert _columns(cursor) == BASE_COLUMNS | ADDED_COLUMNS and _has_index(
                cursor
            )

            # Partly applied the other way (index present, three columns missing): one ALTER restores them.
            cursor.execute(
                "ALTER TABLE signals DROP COLUMN take_profit, DROP COLUMN decision_id, "
                "DROP COLUMN bar_open_time"
            )
            assert _run(cursor, FORWARD) == 1
            assert _columns(cursor) == BASE_COLUMNS | ADDED_COLUMNS and _has_index(
                cursor
            )

            # Partly rolled back (index gone, two columns gone): one ALTER removes what is left.
            cursor.execute(
                "ALTER TABLE signals DROP INDEX uq_signals_signal_key, DROP COLUMN stop_loss, "
                "DROP COLUMN bar_close_time"
            )
            assert _run(cursor, ROLLBACK) == 1
            assert _columns(cursor) == BASE_COLUMNS and not _has_index(cursor)
    finally:
        if owns_container:
            subprocess.run(
                ["docker", "rm", "-f", container_name], check=False, capture_output=True
            )
