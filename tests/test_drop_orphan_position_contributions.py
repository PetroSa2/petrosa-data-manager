"""Unit tests for `data_manager.maintenance.drop_orphan_position_contributions`.

The tests use SQLite in-memory engines so they exercise the real SQLAlchemy
code path (table existence check, row count, DROP TABLE) end-to-end without
needing a live MySQL instance. The schema check is mocked when the DDL
difference between SQLite and MySQL matters (the production query uses
`information_schema.tables`, which SQLite doesn't have).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlalchemy as sa

from data_manager.maintenance import drop_orphan_position_contributions as mod


def _create_engine_with_table(rows: int = 0) -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    sa.Table(
        mod.TARGET_TABLE,
        metadata,
        sa.Column("contribution_id", sa.String(64), primary_key=True),
    )
    metadata.create_all(engine)
    if rows > 0:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"INSERT INTO {mod.TARGET_TABLE} (contribution_id) VALUES (:c)"
                ),
                [{"c": f"row-{i}"} for i in range(rows)],
            )
    return engine


def _create_engine_without_table() -> sa.Engine:
    return sa.create_engine("sqlite+pysqlite:///:memory:", future=True)


def _patch_table_exists(exists: bool):
    return patch.object(mod, "table_exists", return_value=exists)


def test_dry_run_table_absent_is_noop():
    engine = _create_engine_without_table()
    with _patch_table_exists(False):
        result = mod.execute_migration(engine, dry_run=True)
    assert result["table_existed"] is False
    assert result["dropped"] is False
    assert result["dry_run"] is True


def test_dry_run_table_present_zero_rows_does_not_drop():
    engine = _create_engine_with_table(rows=0)
    with _patch_table_exists(True):
        result = mod.execute_migration(engine, dry_run=True)
    assert result["table_existed"] is True
    assert result["row_count"] == 0
    assert result["dropped"] is False
    # Verify the table is still there post-dry-run.
    insp = sa.inspect(engine)
    assert mod.TARGET_TABLE in insp.get_table_names()


def test_apply_zero_rows_drops_table():
    engine = _create_engine_with_table(rows=0)
    with _patch_table_exists(True):
        result = mod.execute_migration(engine, dry_run=False)
    assert result["table_existed"] is True
    assert result["row_count"] == 0
    assert result["dropped"] is True
    insp = sa.inspect(engine)
    assert mod.TARGET_TABLE not in insp.get_table_names()


def test_apply_with_rows_raises_guard_and_keeps_table():
    engine = _create_engine_with_table(rows=3)
    with _patch_table_exists(True):  # noqa: SIM117
        with pytest.raises(mod.RowCountGuardError) as ei:
            mod.execute_migration(engine, dry_run=False)
    assert "rows=3" in str(ei.value)
    insp = sa.inspect(engine)
    assert mod.TARGET_TABLE in insp.get_table_names()


def test_dry_run_with_rows_does_not_raise_and_keeps_table():
    engine = _create_engine_with_table(rows=5)
    with _patch_table_exists(True):
        result = mod.execute_migration(engine, dry_run=True)
    assert result["row_count"] == 5
    assert result["dropped"] is False
    insp = sa.inspect(engine)
    assert mod.TARGET_TABLE in insp.get_table_names()


def test_main_requires_mode_flag(capsys):
    with pytest.raises(SystemExit) as ei:
        mod.main([])
    assert ei.value.code == 2  # argparse exits 2 on missing required arg


def test_main_dry_run_no_mysql_uri_returns_2(monkeypatch):
    monkeypatch.delenv("MYSQL_URI", raising=False)
    assert mod.main(["--dry-run"]) == 2


def test_main_dry_run_with_engine_factory(monkeypatch):
    engine = _create_engine_with_table(rows=0)

    def _factory():
        return engine

    monkeypatch.setattr(mod, "_make_engine_from_env", _factory)
    with _patch_table_exists(True):
        rc = mod.main(["--dry-run"])
    assert rc == 0


def test_main_apply_row_count_guard_returns_3(monkeypatch):
    engine = _create_engine_with_table(rows=2)

    def _factory():
        return engine

    monkeypatch.setattr(mod, "_make_engine_from_env", _factory)
    with _patch_table_exists(True):
        rc = mod.main(["--apply"])
    assert rc == 3


def test_drop_sql_constant_is_idempotent():
    """Belt-and-braces: the DROP statement uses IF EXISTS so reruns are safe."""
    assert "IF EXISTS" in mod.DROP_SQL


def test_target_table_is_position_contributions():
    """Lock the target table name — any future rename must update the audit doc."""
    assert mod.TARGET_TABLE == "position_contributions"
    assert mod.TARGET_SCHEMA == "petrosa_crypto"
