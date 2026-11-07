"""MySQL 5.7 contract for migration 020 (PetroSa2/petrosa-data-manager#880).

Production MySQL is 5.7, which rejected 013's ADD COLUMN IF NOT EXISTS, so 013's columns and status values never
existed there. 020 adds them with an information_schema guard, in one online ALTER, and appends the status values.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "020_trading_state_columns_mysql57.sql"
ROLLBACK = MIGRATIONS / "020_trading_state_columns_mysql57_rollback.sql"

ADDED_COLUMNS = {
    "entry_commission",
    "fee_status",
    "closed_by_strategy_id",
    "close_actor",
    "idempotency_key",
    "decision_id",
}
OLD_STATUS = "enum('open','closed')"
NEW_STATUS = "enum('open','closed','partially_closed','superseded')"

# The production positions table on 2026-11-07 (information_schema, MySQL 5.7.32), in column order.
PRODUCTION_POSITIONS = """
CREATE TABLE positions (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  position_id VARCHAR(255) NOT NULL, strategy_id VARCHAR(255), exchange VARCHAR(50), symbol VARCHAR(50),
  position_side ENUM('LONG','SHORT'), entry_price DECIMAL(20,8), quantity DECIMAL(20,8), entry_time DATETIME,
  stop_loss DECIMAL(20,8), take_profit DECIMAL(20,8), status ENUM('open','closed') NOT NULL DEFAULT 'open',
  metadata JSON, exchange_position_id VARCHAR(255), entry_order_id VARCHAR(255), entry_trade_ids JSON,
  stop_loss_order_id VARCHAR(255), take_profit_order_id VARCHAR(255), commission_asset VARCHAR(20),
  commission_total DECIMAL(20,8), exit_price DECIMAL(20,8), exit_time DATETIME, exit_order_id VARCHAR(255),
  exit_trade_ids JSON, pnl DECIMAL(20,8), pnl_pct DECIMAL(10,4), pnl_after_fees DECIMAL(20,8),
  duration_seconds INT, close_reason VARCHAR(50), final_commission DECIMAL(20,8),
  created_at TIMESTAMP NULL DEFAULT NULL, updated_at TIMESTAMP NULL DEFAULT NULL,
  pnl_unknown TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY uq_positions_position_id (position_id)
) ENGINE=InnoDB
"""


def _code(path: Path) -> str:
    """The file without its comment lines."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def _statements(path: Path) -> list[str]:
    return [part.strip() for part in _code(path).split(";") if part.strip()]


def test_020_is_additive_guarded_and_has_rollback():
    forward = _code(FORWARD).upper()
    rollback = _code(ROLLBACK).upper()
    assert "IF NOT EXISTS" not in forward.replace("CREATE TABLE IF NOT EXISTS", "")
    assert "INFORMATION_SCHEMA.COLUMNS" in forward
    for word in ("DROP TABLE", "TRUNCATE", "DELETE ", "DROP COLUMN"):
        assert word not in forward
    assert "INFORMATION_SCHEMA.COLUMNS" in rollback
    for word in ("DROP TABLE", "TRUNCATE", "DELETE ", "MODIFY"):
        assert word not in rollback  # the rollback never shrinks the status ENUM


@pytest.mark.parametrize("path", [FORWARD, ROLLBACK], ids=["forward", "rollback"])
def test_each_file_builds_exactly_one_online_alter(path: Path):
    code = _code(path).upper()
    assert code.count("ALTER TABLE") == 1
    assert len(re.findall(r"^PREPARE ", code, re.M)) == 1
    assert "ALGORITHM=INPLACE, LOCK=NONE" in code


def test_status_enum_only_appends_values():
    """5.7 extends an ENUM in place only when the existing values keep their order; never reorder them."""
    old = re.findall(r"'([^']+)'", OLD_STATUS)
    new = re.findall(r"'([^']+)'", NEW_STATUS)
    assert new[: len(old)] == old
    assert NEW_STATUS.replace("'", "''") in FORWARD.read_text()


def _rows(cursor, sql: str) -> list[tuple]:
    cursor.execute(sql)
    return list(cursor.fetchall())


def _columns(cursor) -> dict[str, str]:
    return dict(
        _rows(
            cursor,
            "SELECT column_name, column_type FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'positions'",
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
    cursor.execute("DROP TABLE IF EXISTS positions")
    cursor.execute(PRODUCTION_POSITIONS)
    cursor.execute(
        "INSERT INTO positions (position_id, status, entry_price) VALUES "
        "('p-open', 'open', 1.5), ('p-closed', 'closed', 2.5)"
    )


def _connect(pymysql):
    container_name = "petrosa-trading-state-migration-mysql-57"
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
                "--platform",
                "linux/amd64",
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
    for _ in range(60):
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


def test_020_rehearsal_on_mysql_57():
    pymysql = pytest.importorskip("pymysql")
    connection, owns_container, container_name = _connect(pymysql)
    try:
        if connection is None:
            pytest.fail("MySQL 5.7 container did not become ready")
        with connection, connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            assert cursor.fetchone()[0].startswith("5.7")

            # From the production shape: one online ALTER adds the six columns and appends the status values.
            _fresh_table(cursor)
            base = _columns(cursor)
            assert _run(cursor, FORWARD) == 1
            after = _columns(cursor)
            assert set(after) == set(base) | ADDED_COLUMNS
            assert after["status"] == NEW_STATUS
            # Existing rows keep their status, and the new values are writable.
            assert sorted(
                _rows(cursor, "SELECT position_id, status FROM positions")
            ) == [
                ("p-closed", "closed"),
                ("p-open", "open"),
            ]
            cursor.execute(
                "INSERT INTO positions (position_id, status, fee_status, close_actor, decision_id) "
                "VALUES ('p-partial', 'partially_closed', 'known', 'reconciler', 'd1')"
            )
            assert _rows(
                cursor, "SELECT status FROM positions WHERE position_id = 'p-partial'"
            ) == [("partially_closed",)]

            # Rerun: nothing is missing, so no ALTER runs.
            assert _run(cursor, FORWARD) == 0
            assert _columns(cursor) == after
            assert _rows(
                cursor,
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE migration_id = '020_trading_state_columns_mysql57'",
            ) == [(1,)]

            # Rollback drops only the added columns, keeps the ENUM; a rerun runs none.
            cursor.execute("DELETE FROM positions WHERE position_id = 'p-partial'")
            assert _run(cursor, ROLLBACK) == 1
            rolled_back = _columns(cursor)
            assert set(rolled_back) == set(base)
            assert rolled_back["status"] == NEW_STATUS
            assert _run(cursor, ROLLBACK) == 0

            # Partly applied (two columns already there, ENUM old): one ALTER adds only the rest.
            _fresh_table(cursor)
            cursor.execute(
                "ALTER TABLE positions ADD COLUMN fee_status VARCHAR(20) NULL, "
                "ADD COLUMN decision_id VARCHAR(64) NULL"
            )
            assert _run(cursor, FORWARD) == 1
            assert set(_columns(cursor)) == set(base) | ADDED_COLUMNS
            assert _columns(cursor)["status"] == NEW_STATUS

            # No positions table at all: the migration runs no ALTER and does not fail.
            cursor.execute("DROP TABLE positions")
            assert _run(cursor, FORWARD) == 0
    finally:
        if owns_container:
            subprocess.run(
                ["docker", "rm", "-f", container_name], check=False, capture_output=True
            )
