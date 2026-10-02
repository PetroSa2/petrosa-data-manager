"""Static migration contract for the durable signal schema."""

from pathlib import Path

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "016_signals_point_in_time.sql"
ROLLBACK = MIGRATIONS / "016_signals_point_in_time_rollback.sql"


def test_signal_migration_is_additive_and_has_rollback():
    assert FORWARD.is_file()
    assert ROLLBACK.is_file()
    forward = FORWARD.read_text().upper()
    assert "ADD COLUMN IF NOT EXISTS SIGNAL_KEY" in forward
    assert "CREATE UNIQUE INDEX UQ_SIGNALS_SIGNAL_KEY" in forward
    assert "DROP TABLE" not in forward
    assert "TRUNCATE" not in forward
    assert "DELETE " not in forward


def test_signal_rollback_targets_only_added_schema():
    rollback = ROLLBACK.read_text().upper()
    assert "DROP INDEX UQ_SIGNALS_SIGNAL_KEY" in rollback
    assert "DROP COLUMN SIGNAL_KEY" in rollback
    assert "DROP TABLE" not in rollback
    assert "DELETE FROM" not in rollback
