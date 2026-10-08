"""Structural and fixture checks for data-manager#344."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
MIGRATIONS = ROOT / "data_manager" / "scripts" / "migrations"

EXPECTED = {
    "idx_klines_m5_symbol_timestamp",
    "idx_klines_m30_symbol_timestamp",
    "idx_klines_h1_symbol_timestamp",
    "idx_klines_d1_symbol_timestamp",
    "idx_audit_logs_dataset_timestamp",
    "idx_audit_logs_symbol",
    "idx_health_metrics_dataset_timestamp",
    "idx_health_metrics_symbol",
    "idx_backfill_jobs_status",
    "idx_backfill_jobs_symbol",
    "idx_strategy",
    "idx_symbol_period",
    "idx_entry_time",
    "idx_exchange",
    "idx_positions_status_entry_time",
    "idx_strategy_id",
    "idx_symbol",
    "idx_klines_m1_open_time",
    "idx_klines_m1_timestamp",
    "idx_klines_15m_open_time",
    "idx_klines_m30_open_time",
    "idx_klines_d1_timestamp",
    "idx_klines_h4_open_time",
    "idx_klines_h4_timestamp",
    "idx_status",
}


def _index_names(text):
    return set(
        re.findall(
            r"(?:INDEX_NAME\s*=\s*'|DROP INDEX\s+|ADD INDEX\s+)([a-zA-Z0-9_]+)",
            text,
        )
    )


def test_forward_and_rollback_cover_exact_index_set():
    forward = (MIGRATIONS / "007_drop_duplicate_and_unused_indexes.sql").read_text()
    rollback = (
        MIGRATIONS / "007_rollback_drop_duplicate_and_unused_indexes.sql"
    ).read_text()

    assert _index_names(forward) == EXPECTED
    assert _index_names(rollback) == EXPECTED


def test_migrations_are_mysql57_idempotent_and_online():
    for name in (
        "007_drop_duplicate_and_unused_indexes.sql",
        "007_rollback_drop_duplicate_and_unused_indexes.sql",
    ):
        text = (MIGRATIONS / name).read_text()
        ddl_count = len(re.findall(r"ALTER TABLE", text))
        assert ddl_count > 0
        assert text.count("ALGORITHM=INPLACE, LOCK=NONE") == ddl_count
        assert "IF EXISTS" not in text
        assert "INFORMATION_SCHEMA.STATISTICS" in text
        assert "PREPARE" in text
        assert "EXECUTE" in text
        assert "DEALLOCATE PREPARE" in text
