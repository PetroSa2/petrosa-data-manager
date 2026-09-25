"""Unit tests for the guarded legacy-table drop migration."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlalchemy as sa

from data_manager.maintenance import (
    drop_orphan_petrosa_crypto_tables_2026_09 as mod,
)


def _make_engine(existing_tables: dict[str, int]) -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    for table_name in existing_tables:
        sa.Table(table_name, metadata, sa.Column("id", sa.String(64), primary_key=True))
    metadata.create_all(engine)
    for table_name, rows in existing_tables.items():
        if rows:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(f"INSERT INTO {table_name} (id) VALUES (:c)"),
                    [{"c": f"row-{i}"} for i in range(rows)],
                )
    return engine


def _patch_table_exists(existing: set[str]):
    def _fake_exists(engine, table, *, schema=mod.TARGET_SCHEMA):
        return table in existing

    return patch.object(mod, "table_exists", side_effect=_fake_exists)


def test_target_tables_keep_only_legacy_metadata():
    assert mod.TARGET_TABLES == ("extraction_metadata",)
    assert "trades" not in mod.TARGET_TABLES
    assert "funding_rates" not in mod.TARGET_TABLES
    assert "strategy_positions" not in mod.TARGET_TABLES


def test_dry_run_absent_is_noop():
    engine = _make_engine({})
    with _patch_table_exists(set()):
        results = mod.execute_migration(engine, dry_run=True)
    assert results == [
        {
            "target_schema": "petrosa_crypto",
            "target_table": "extraction_metadata",
            "dry_run": True,
            "table_existed": False,
            "row_count": 0,
            "dropped": False,
            "guard_tripped": False,
            "sql_planned": "DROP TABLE IF EXISTS extraction_metadata",
        }
    ]


def test_dry_run_does_not_drop_zero_row_table():
    engine = _make_engine({"extraction_metadata": 0})
    with _patch_table_exists({"extraction_metadata"}):
        results = mod.execute_migration(engine, dry_run=True)
    assert results[0]["table_existed"] is True
    assert results[0]["dropped"] is False
    assert "extraction_metadata" in sa.inspect(engine).get_table_names()


def test_apply_zero_row_table_drops_it():
    engine = _make_engine({"extraction_metadata": 0})
    with _patch_table_exists({"extraction_metadata"}):
        results = mod.execute_migration(engine, dry_run=False)
    assert results[0]["dropped"] is True
    assert "extraction_metadata" not in sa.inspect(engine).get_table_names()


def test_apply_skips_table_with_rows():
    engine = _make_engine({"extraction_metadata": 5})
    with _patch_table_exists({"extraction_metadata"}):
        results = mod.execute_migration(engine, dry_run=False)
    assert results[0]["dropped"] is False
    assert results[0]["guard_tripped"] is True
    assert results[0]["row_count"] == 5
    assert "extraction_metadata" in sa.inspect(engine).get_table_names()


def test_drop_sql_is_idempotent():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    with _patch_table_exists(set()):
        result = mod.execute_migration_for_table(
            engine, "extraction_metadata", dry_run=True
        )
    assert "IF EXISTS" in str(result["sql_planned"])


def test_main_requires_mode_flag():
    with pytest.raises(SystemExit) as error:
        mod.main([])
    assert error.value.code == 2


def test_main_dry_run_no_mysql_uri_returns_2(monkeypatch):
    monkeypatch.delenv("MYSQL_URI", raising=False)
    assert mod.main(["--dry-run"]) == 2


def test_main_dry_run_with_engine_factory(monkeypatch):
    engine = _make_engine({"extraction_metadata": 0})
    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    with _patch_table_exists({"extraction_metadata"}):
        assert mod.main(["--dry-run"]) == 0


def test_main_apply_returns_3_when_guard_trips(monkeypatch):
    engine = _make_engine({"extraction_metadata": 1})
    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    with _patch_table_exists({"extraction_metadata"}):
        assert mod.main(["--apply"]) == 3


def test_main_apply_clean_returns_0(monkeypatch):
    engine = _make_engine({"extraction_metadata": 0})
    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    with _patch_table_exists({"extraction_metadata"}):
        assert mod.main(["--apply"]) == 0


def test_main_table_filter_cli_flag(monkeypatch):
    engine = _make_engine({"extraction_metadata": 0})
    captured: dict[str, object] = {}
    real_execute = mod.execute_migration

    def capture(engine_arg, *, dry_run, tables=mod.TARGET_TABLES):
        captured["tables"] = tables
        return real_execute(engine_arg, dry_run=dry_run, tables=tables)

    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    monkeypatch.setattr(mod, "execute_migration", capture)
    with _patch_table_exists({"extraction_metadata"}):
        assert mod.main(["--apply", "--table", "extraction_metadata"]) == 0
    assert captured["tables"] == ("extraction_metadata",)


def test_main_rejects_removed_table(monkeypatch):
    monkeypatch.setenv("MYSQL_URI", "sqlite+pysqlite:///:memory:")
    with pytest.raises(SystemExit) as error:
        mod.main(["--dry-run", "--table", "funding_rates"])
    assert error.value.code == 2
