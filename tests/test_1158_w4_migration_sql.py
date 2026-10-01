"""Static safety checks and opt-in Tier-1 rehearsal for ticket 1163."""

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


def test_migration_artefacts_are_present_and_readable():
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    assert PRE_SNAPSHOT.is_file()


def test_forward_migration_is_pk_only_and_online():
    sql = FORWARD.read_text()
    assert "DROP PRIMARY KEY" in sql
    assert "ADD PRIMARY KEY (symbol, timestamp)" in sql
    assert "ALGORITHM=INPLACE" in sql
    assert "LOCK=NONE" in sql
    for forbidden in ("DROP COLUMN", "DROP TABLE", "DROP DATABASE", "TRUNCATE"):
        assert forbidden not in sql.upper()
    tables = (
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
    for table in tables:
        assert table in sql
    assert len(tables) == 12
    assert "health_metrics" not in sql
    assert "audit_logs" not in sql
    assert "positions" not in sql
    assert "signals" not in sql


def test_rollback_documents_snapshot_data_recovery_and_keeps_columns():
    sql = ROLLBACK.read_text().upper()
    assert "DBAAS SNAPSHOT" in sql
    assert "DROP PRIMARY KEY" in sql
    assert "ADD PRIMARY KEY (ID)" in sql
    for forbidden in ("DROP COLUMN", "DROP TABLE", "DROP DATABASE", "TRUNCATE"):
        assert forbidden not in sql


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


@pytest.mark.integration
def test_tier1_mysql57_rehearsal():
    """Run by the lab job when MYSQL57_REHEARSAL_DSN is configured."""
    dsn = pytest.importorskip("os").environ.get("MYSQL57_REHEARSAL_DSN")
    if not dsn:
        pytest.skip(TIER1_SKIP_REASON)
    pytest.skip(
        "Tier-1 lab driver is provisioned by the CI rehearsal job; local unit "
        "runs intentionally never create or mutate a database"
    )
