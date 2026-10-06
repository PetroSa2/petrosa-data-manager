"""Static safety checks for the operator-run ticket 527 migration."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "018_klines_drop_duplicate_pk_indexes.sql"
ROLLBACK = MIGRATIONS / "018_klines_drop_duplicate_pk_indexes_rollback.sql"
SNAPSHOT = MIGRATIONS / "018_klines_duplicate_pk_index_snapshot.sql"
INDEXES = {
    "uniq_klines_m5_symbol_timestamp",
    "uniq_klines_m15_symbol_timestamp",
    "idx_klines_15m_symbol_timestamp",
    "uniq_klines_m30_symbol_timestamp",
    "uniq_klines_h1_symbol_timestamp",
    "uniq_klines_d1_symbol_timestamp",
}
TABLES = {"klines_m5", "klines_m15", "klines_m30", "klines_h1", "klines_d1"}


def _index_names(sql: str) -> set[str]:
    return set(
        re.findall(
            r"(?:INDEX_NAME\s*=\s*'|DROP INDEX\s+|ADD (?:UNIQUE )?INDEX\s+)([a-zA-Z0-9_]+)",
            sql,
        )
    )


def test_migration_files_exist_and_cover_exact_indexes() -> None:
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    assert SNAPSHOT.is_file()
    assert _index_names(FORWARD.read_text()) == INDEXES
    assert _index_names(ROLLBACK.read_text()) == INDEXES


def test_forward_is_mysql57_safe_online_and_preserves_primary_keys() -> None:
    sql = FORWARD.read_text()
    assert sql.count("INFORMATION_SCHEMA.STATISTICS") == len(INDEXES)
    assert sql.count("ALTER TABLE") == len(TABLES)
    assert sql.count("ALGORITHM=INPLACE, LOCK=NONE") == len(TABLES)
    assert "DROP PRIMARY KEY" not in sql.upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP DATABASE", "TRUNCATE"):
        assert forbidden not in sql.upper()
    assert "PREPARE" in sql
    assert "EXECUTE" in sql
    assert "DEALLOCATE PREPARE" in sql


def test_rollback_restores_original_index_types_and_is_online() -> None:
    sql = ROLLBACK.read_text()
    assert sql.count("INFORMATION_SCHEMA.STATISTICS") == len(INDEXES)
    assert sql.count("ALTER TABLE") == len(TABLES)
    assert sql.count("ALGORITHM=INPLACE, LOCK=NONE") == len(TABLES)
    assert sql.count("ADD UNIQUE INDEX") == 5
    assert sql.count("ADD INDEX") == 1
    assert "DROP PRIMARY KEY" not in sql.upper()


def test_snapshot_is_read_only_and_reports_pages_and_bytes() -> None:
    sql = SNAPSHOT.read_text().upper()
    assert "MYSQL.INNODB_INDEX_STATS" in sql
    assert "INNODB_PAGE_SIZE" in sql
    assert "INDEX_PAGES" in sql
    assert "INDEX_SIZE_BYTES" in sql
    assert "SELECT" in sql
    for forbidden in ("ALTER ", "CREATE ", "DROP ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in sql
    assert all(index.upper() in sql for index in INDEXES)
