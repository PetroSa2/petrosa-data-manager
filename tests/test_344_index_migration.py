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


def test_snapshot_fixture_is_explicitly_pending_and_complete():
    query = (MIGRATIONS / "007_pre_snapshot.sql").read_text()
    baseline = (MIGRATIONS / "007_pre_snapshot_baseline.txt").read_text()

    assert "PENDING-OPERATOR" in baseline
    assert "SHOW CREATE TABLE" in query
    assert EXPECTED <= set(re.findall(r"(?:KEY|INDEX)\s+([a-zA-Z0-9_]+)", baseline))


def test_tier1_transcript_is_present_and_machine_evidenced():
    transcript = (MIGRATIONS / "007_tier1_rehearsal.log").read_text()

    assert transcript
    assert "ERROR " not in transcript
    assert "is not supported" not in transcript
    assert re.search(r"5\.7\.\d+", transcript)
    assert "PHASE: apply" in transcript
    assert "PHASE: re-apply" in transcript
    assert "PHASE: rollback" in transcript
    assert transcript.index("PHASE: apply") < transcript.index("PHASE: re-apply")
    assert transcript.index("PHASE: re-apply") < transcript.index("PHASE: rollback")
    assert transcript.index("PHASE: rollback") < transcript.rindex("PHASE: re-apply")
    assert EXPECTED <= set(
        re.findall(r"(?:DROP|ADD) INDEX\s+([a-zA-Z0-9_]+)", transcript)
    )
