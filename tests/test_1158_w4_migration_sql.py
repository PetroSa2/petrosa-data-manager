"""Static safety checks and opt-in Tier-1 rehearsal for ticket 518."""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path

import pytest

MIGRATION_DIR = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATION_DIR / "009_klines_pk_and_column_redesign.sql"
ROLLBACK = MIGRATION_DIR / "009_klines_pk_and_column_redesign_rollback.sql"
PRE_SNAPSHOT = MIGRATION_DIR / "009_klines_pk_and_column_redesign_pre_snapshot.sql"
TIER1_SKIP_REASON = (
    "Tier-1 rehearsal requires an explicitly configured local MySQL 5.7 lab; "
    "no production database is ever used"
)
TABLES = (
    "klines_m1",
    "klines_m3",
    "klines_m5",
    "klines_m15",
    "klines_m30",
    "klines_h1",
    "klines_h2",
    "klines_h4",
    "klines_h6",
    "klines_h8",
    "klines_h12",
    "klines_d1",
)
REHEARSAL_TABLES = ("klines_m1", "klines_m15")
ORIGINAL_COLUMNS = {"id", "symbol", "timestamp", "close_price"}


def test_migration_artefacts_are_present_and_readable():
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    assert PRE_SNAPSHOT.is_file()


def test_forward_migration_is_guarded_pk_only_and_online():
    sql = FORWARD.read_text()
    assert "DROP PRIMARY KEY" in sql
    assert "ADD PRIMARY KEY (symbol, timestamp)" in sql
    assert "ALGORITHM=INPLACE" in sql
    assert "LOCK=NONE" in sql
    for forbidden in ("DROP COLUMN", "DROP TABLE", "DROP DATABASE", "TRUNCATE"):
        assert forbidden not in sql.upper()
    for table in TABLES:
        assert table in sql
    assert len(TABLES) == 12
    assert sql.upper().count("INFORMATION_SCHEMA.TABLES") == len(TABLES)
    assert sql.upper().count("TABLE_TYPE = 'BASE TABLE'") == len(TABLES)
    assert "health_metrics" not in sql
    assert "audit_logs" not in sql
    assert "positions" not in sql
    assert "signals" not in sql


def test_rollback_is_guarded_idempotent_and_keeps_columns():
    sql = ROLLBACK.read_text().upper()
    assert "DBAAS SNAPSHOT" in sql
    assert "DROP PRIMARY KEY" in sql
    assert "ADD PRIMARY KEY (ID)" in sql
    for forbidden in ("DROP COLUMN", "DROP TABLE", "DROP DATABASE", "TRUNCATE"):
        assert forbidden not in sql
    assert sql.count("INFORMATION_SCHEMA.TABLES") == len(TABLES)
    assert sql.count("TABLE_TYPE = 'BASE TABLE'") == len(TABLES)


def test_pre_snapshot_is_read_only():
    sql = PRE_SNAPSHOT.read_text().upper()
    assert "INFORMATION_SCHEMA" in sql
    for forbidden in (
        "ALTER ",
        "CREATE ",
        "DROP ",
        "TRUNCATE",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ):
        assert forbidden not in sql


def test_tier1_skip_reason_is_explicit():
    assert "MYSQL 5.7" in TIER1_SKIP_REASON.upper()
    assert "PRODUCTION" in TIER1_SKIP_REASON.upper()


def _code(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def _statements(path: Path) -> list[str]:
    return [part.strip() for part in _code(path).split(";") if part.strip()]


def _connect(pymysql):
    container_name = "petrosa-klines-migration-mysql-57"
    external_host = os.getenv("MYSQL_REHEARSAL_HOST")
    owns_container = external_host is None
    port = int(os.getenv("MYSQL_REHEARSAL_PORT", "3306"))
    if owns_container:
        subprocess.run(
            ["docker", "rm", "-f", container_name], check=False, capture_output=True
        )
        docker_command = [
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
        ]
        if platform.machine().lower() in {"aarch64", "arm64"}:
            docker_command[2:2] = ["--platform", "linux/amd64"]
        docker = subprocess.run(
            docker_command,
            capture_output=True,
            text=True,
        )
        if docker.returncode != 0:
            pytest.skip("Docker is unavailable for the local MySQL 5.7 rehearsal")
        try:
            port_result = subprocess.run(
                ["docker", "port", container_name, "3306/tcp"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                ["docker", "rm", "-f", container_name], check=False, capture_output=True
            )
            pytest.skip(
                "The local MySQL 5.7 image is unavailable on this host architecture"
            )
        port = int(port_result.stdout.splitlines()[0].rsplit(":", 1)[1].strip())
    connection = None
    for _ in range(90):
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


def _run(cursor, path: Path) -> int:
    cursor.execute("SHOW GLOBAL STATUS LIKE 'Com_alter_table'")
    before = int(cursor.fetchone()[1])
    for statement in _statements(path):
        cursor.execute(statement)
    cursor.execute("SHOW GLOBAL STATUS LIKE 'Com_alter_table'")
    return int(cursor.fetchone()[1]) - before


def _fresh_tables(cursor) -> None:
    for table in TABLES:
        cursor.execute(f"DROP TABLE IF EXISTS {table}")
    for table in REHEARSAL_TABLES:
        cursor.execute(
            f"CREATE TABLE {table} ("
            "id BIGINT NOT NULL, symbol VARCHAR(32) NOT NULL, "
            "timestamp DATETIME NOT NULL, close_price DECIMAL(20, 8) NOT NULL, "
            "PRIMARY KEY (id))"
        )
        cursor.execute(
            f"INSERT INTO {table} (id, symbol, timestamp, close_price) "
            "VALUES (1, 'BTCUSDT', '2026-01-01 00:00:00', 1.25), "
            "(2, 'ETHUSDT', '2026-01-01 00:01:00', 2.50)"
        )


def _columns(cursor, table: str) -> set[str]:
    cursor.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {row[0] for row in cursor.fetchall()}


def _primary_key(cursor, table: str) -> tuple[str, ...]:
    cursor.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
        "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
        (table,),
    )
    return tuple(row[0] for row in cursor.fetchall())


def _has_unique_id(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "AND INDEX_NAME = %s AND NON_UNIQUE = 0 AND COLUMN_NAME = 'id'",
        (table, f"uq_{table}_id"),
    )
    return cursor.fetchone()[0] == 1


def _row_count(cursor, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    return cursor.fetchone()[0]


@pytest.mark.integration
def test_tier1_mysql57_rehearsal():
    """Run the migration, rerun it, roll it back, and rerun the rollback."""
    pymysql = pytest.importorskip("pymysql")
    connection, owns_container, container_name = _connect(pymysql)
    try:
        if connection is None:
            pytest.fail("MySQL 5.7 container did not become ready")
        with connection, connection.cursor() as cursor:
            _fresh_tables(cursor)
            assert _run(cursor, FORWARD) == len(REHEARSAL_TABLES)
            for table in REHEARSAL_TABLES:
                assert _columns(cursor, table) == ORIGINAL_COLUMNS
                assert _primary_key(cursor, table) == ("symbol", "timestamp")
                assert _has_unique_id(cursor, table)
                assert _row_count(cursor, table) == 2
            cursor.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'klines\\_%' "
                "ORDER BY TABLE_NAME"
            )
            assert {row[0] for row in cursor.fetchall()} == set(REHEARSAL_TABLES)
            assert _run(cursor, FORWARD) == 0
            assert _run(cursor, ROLLBACK) == len(REHEARSAL_TABLES)
            for table in REHEARSAL_TABLES:
                assert _columns(cursor, table) == ORIGINAL_COLUMNS
                assert _primary_key(cursor, table) == ("id",)
                assert not _has_unique_id(cursor, table)
                assert _row_count(cursor, table) == 2
            assert _run(cursor, ROLLBACK) == 0
    finally:
        if owns_container:
            subprocess.run(
                ["docker", "rm", "-f", container_name], check=False, capture_output=True
            )
