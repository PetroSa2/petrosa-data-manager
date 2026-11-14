"""MySQL 5.7 contract and opt-in rehearsal for migration 003."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "003_klines_drop_redundant_index.sql"
ROLLBACK = MIGRATIONS / "003_klines_drop_redundant_index_rollback.sql"
TABLES = ("klines_m5", "klines_m15", "klines_m30", "klines_h1", "klines_d1")
TARGETS = {f"idx_{table}_open_time" for table in TABLES}


def _code(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def _statements(path: Path) -> list[str]:
    return [part.strip() for part in _code(path).split(";") if part.strip()]


def test_migration_is_mysql57_safe_and_covers_all_targets():
    for path in (FORWARD, ROLLBACK):
        sql = _code(path).upper()
        assert "IF EXISTS" not in sql
        assert sql.count("INFORMATION_SCHEMA.STATISTICS") == len(TABLES)
        assert sql.count("ALGORITHM=INPLACE, LOCK=NONE") == len(TABLES)
        assert sql.count("ALTER TABLE") == len(TABLES)
        assert "PREPARE STMT FROM @ALTER_SQL" in sql
        assert "EXECUTE STMT" in sql
        assert "DEALLOCATE PREPARE STMT" in sql
        assert {
            name.lower() for name in re.findall(r"INDEX_NAME = '([^']+)'", sql)
        } == TARGETS


def _connect(pymysql):
    container_name = "petrosa-klines-index-migration-mysql-57"
    external_host = os.getenv("MYSQL_REHEARSAL_HOST")
    owns_container = external_host is None
    port = int(os.getenv("MYSQL_REHEARSAL_PORT", "3306"))
    if owns_container:
        subprocess.run(
            ["docker", "rm", "-f", container_name], check=False, capture_output=True
        )
        command = [
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
            command[2:2] = ["--platform", "linux/amd64"]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            pytest.skip("Docker is unavailable for the local MySQL 5.7 rehearsal")
            return None, owns_container, container_name
        if result.returncode != 0:
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
            return None, owns_container, container_name
        port = int(port_result.stdout.splitlines()[0].rsplit(":", 1)[1])
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
        cursor.execute(
            f"CREATE TABLE {table} (id BIGINT NOT NULL, open_time DATETIME NOT NULL, "
            "PRIMARY KEY (id), INDEX idx_%s_open_time (open_time))" % table
        )
        cursor.execute(
            f"INSERT INTO {table} (id, open_time) VALUES "
            "(1, '2026-01-01 00:00:00'), (2, '2026-01-01 00:01:00')"
        )


def _indexes(cursor) -> set[str]:
    cursor.execute(
        "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND INDEX_NAME LIKE 'idx_klines_%_open_time'"
    )
    return {row[0] for row in cursor.fetchall()}


@pytest.mark.integration
def test_mysql57_rehearsal_drops_reruns_and_restores_indexes():
    pymysql = pytest.importorskip("pymysql")
    connection, owns_container, container_name = _connect(pymysql)
    try:
        if connection is None:
            pytest.fail("MySQL 5.7 container did not become ready")
        assert connection is not None
        with connection, connection.cursor() as cursor:
            _fresh_tables(cursor)
            assert _indexes(cursor) == TARGETS
            assert _run(cursor, FORWARD) == len(TABLES)
            assert _indexes(cursor) == set()
            assert _run(cursor, FORWARD) == 0
            assert _run(cursor, ROLLBACK) == len(TABLES)
            assert _indexes(cursor) == TARGETS
            assert _run(cursor, ROLLBACK) == 0
    finally:
        if owns_container:
            subprocess.run(
                ["docker", "rm", "-f", container_name], check=False, capture_output=True
            )
