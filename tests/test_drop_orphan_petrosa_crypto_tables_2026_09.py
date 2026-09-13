"""Unit tests for `data_manager.maintenance.drop_orphan_petrosa_crypto_tables_2026_09`.

Mirrors the SQLite in-memory approach from
`tests/test_drop_orphan_position_contributions.py`: real SQLAlchemy code
path (existence check, row count, DROP TABLE) without a live MySQL
instance. `table_exists` is mocked because the production query targets
`information_schema.tables`, which SQLite doesn't have.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import sqlalchemy as sa

from data_manager.maintenance import (
    drop_orphan_petrosa_crypto_tables_2026_09 as mod,
)


def _make_engine(existing_tables: dict[str, int]) -> sa.Engine:
    """Build a SQLite engine with the given tables pre-populated with
    `rows` empty-schema rows each (keyed by table name -> row count)."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    for table_name in existing_tables:
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.String(64), primary_key=True),
        )
    metadata.create_all(engine)
    for table_name, rows in existing_tables.items():
        if rows > 0:
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


def test_target_tables_are_the_six_confirmed_dead_tables():
    """Lock the target list — matches the AC1 evidence doc's six tables.
    `datasets` / `lineage_records` (AC5 — retained) must never appear."""
    assert set(mod.TARGET_TABLES) == {
        "strategy_positions",
        "exchange_positions",
        "position_contributions",
        "extraction_metadata",
        "trades",
        "funding_rates",
    }
    assert "datasets" not in mod.TARGET_TABLES
    assert "lineage_records" not in mod.TARGET_TABLES
    assert mod.TARGET_SCHEMA == "petrosa_crypto"


def test_dry_run_all_tables_absent_is_noop():
    engine = _make_engine({})
    with _patch_table_exists(set()):
        results = mod.execute_migration(engine, dry_run=True)
    assert len(results) == len(mod.TARGET_TABLES)
    assert all(r["table_existed"] is False for r in results)
    assert all(r["dropped"] is False for r in results)


def test_dry_run_zero_row_tables_report_would_drop_but_do_not():
    existing = {t: 0 for t in mod.TARGET_TABLES}
    engine = _make_engine(existing)
    with _patch_table_exists(set(existing)):
        results = mod.execute_migration(engine, dry_run=True)
    assert all(r["table_existed"] is True for r in results)
    assert all(r["row_count"] == 0 for r in results)
    assert all(r["dropped"] is False for r in results)
    assert all(r["guard_tripped"] is False for r in results)
    insp = sa.inspect(engine)
    for t in mod.TARGET_TABLES:
        assert t in insp.get_table_names()


def test_apply_zero_row_tables_all_drop():
    existing = {t: 0 for t in mod.TARGET_TABLES}
    engine = _make_engine(existing)
    with _patch_table_exists(set(existing)):
        results = mod.execute_migration(engine, dry_run=False)
    assert all(r["dropped"] is True for r in results)
    insp = sa.inspect(engine)
    for t in mod.TARGET_TABLES:
        assert t not in insp.get_table_names()


def test_apply_skips_table_with_rows_but_drops_the_rest():
    existing = {t: 0 for t in mod.TARGET_TABLES}
    existing["trades"] = 5
    engine = _make_engine(existing)
    with _patch_table_exists(set(existing)):
        results = mod.execute_migration(engine, dry_run=False)
    by_table = {r["target_table"]: r for r in results}
    assert by_table["trades"]["dropped"] is False
    assert by_table["trades"]["guard_tripped"] is True
    assert by_table["trades"]["row_count"] == 5
    for t in mod.TARGET_TABLES:
        if t != "trades":
            assert by_table[t]["dropped"] is True
    insp = sa.inspect(engine)
    assert "trades" in insp.get_table_names()
    assert "extraction_metadata" not in insp.get_table_names()


def test_restrict_to_single_table():
    existing = {t: 0 for t in mod.TARGET_TABLES}
    engine = _make_engine(existing)
    with _patch_table_exists(set(existing)):
        results = mod.execute_migration(
            engine, dry_run=False, tables=("extraction_metadata",)
        )
    assert len(results) == 1
    assert results[0]["target_table"] == "extraction_metadata"
    insp = sa.inspect(engine)
    assert "extraction_metadata" not in insp.get_table_names()
    assert "trades" in insp.get_table_names()


def test_drop_sql_is_idempotent_per_table():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    with _patch_table_exists(set()):
        for t in mod.TARGET_TABLES:
            result = mod.execute_migration_for_table(engine, t, dry_run=True)
            assert "IF EXISTS" in str(result["sql_planned"])


def test_main_requires_mode_flag():
    with pytest.raises(SystemExit) as ei:
        mod.main([])
    assert ei.value.code == 2  # argparse exits 2 on missing required arg


def test_main_dry_run_no_mysql_uri_returns_2(monkeypatch):
    monkeypatch.delenv("MYSQL_URI", raising=False)
    assert mod.main(["--dry-run"]) == 2


def test_main_dry_run_with_engine_factory(monkeypatch):
    existing = {t: 0 for t in mod.TARGET_TABLES}
    engine = _make_engine(existing)

    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    with _patch_table_exists(set(existing)):
        rc = mod.main(["--dry-run"])
    assert rc == 0


def test_main_apply_returns_3_when_any_table_has_rows(monkeypatch):
    existing = {t: 0 for t in mod.TARGET_TABLES}
    existing["funding_rates"] = 1
    engine = _make_engine(existing)

    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    with _patch_table_exists(set(existing)):
        rc = mod.main(["--apply"])
    assert rc == 3


def test_main_apply_all_clean_returns_0(monkeypatch):
    existing = {t: 0 for t in mod.TARGET_TABLES}
    engine = _make_engine(existing)

    monkeypatch.setattr(mod, "_make_engine_from_env", lambda: engine)
    with _patch_table_exists(set(existing)):
        rc = mod.main(["--apply"])
    assert rc == 0


def test_main_table_filter_cli_flag(monkeypatch):
    # main() disposes its engine on exit, which drops SQLite in-memory state —
    # so this test only verifies the CLI wiring (rc + which tables were
    # targeted), not post-hoc table existence. Table-level drop behavior for
    # a --table subset is covered by test_restrict_to_single_table above
    # (calling execute_migration directly, without main()'s dispose()).
    existing = {t: 0 for t in mod.TARGET_TABLES}
    engine = _make_engine(existing)
    captured: dict[str, object] = {}

    def _factory():
        return engine

    _real_execute_migration = mod.execute_migration

    def _capture_execute_migration(engine_arg, *, dry_run, tables=mod.TARGET_TABLES):
        captured["tables"] = tables
        return _real_execute_migration(engine_arg, dry_run=dry_run, tables=tables)

    monkeypatch.setattr(mod, "_make_engine_from_env", _factory)
    monkeypatch.setattr(mod, "execute_migration", _capture_execute_migration)
    with _patch_table_exists(set(existing)):
        rc = mod.main(["--apply", "--table", "trades", "--table", "funding_rates"])
    assert rc == 0
    assert captured["tables"] == ("trades", "funding_rates")


def test_main_rejects_unknown_table(monkeypatch):
    monkeypatch.setenv("MYSQL_URI", "sqlite+pysqlite:///:memory:")
    with pytest.raises(SystemExit) as ei:
        mod.main(["--dry-run", "--table", "not_a_real_table"])
    assert ei.value.code == 2
