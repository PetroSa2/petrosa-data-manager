"""Tests for petrosa-data-manager#264 — self-managed `daily_pnl` MySQL table.

Prior to this fix, `daily_pnl` was written by petrosa-tradeengine
(`PUT /api/v1/mysql/daily_pnl`) but never provisioned anywhere, so
`MySQLAdapter._get_table("daily_pnl")` fell through to the reflect-or-fail
branch and raised `DatabaseError: Unknown collection or failed to reflect:
daily_pnl` (wrapping SQLAlchemy's `NoSuchTableError`) on every call.
"""

from data_manager.db.mysql_adapter import MySQLAdapter


def _index_names(adapter, table_name):
    table = adapter.tables[table_name]
    return {idx.name for idx in table.indexes}


def test_daily_pnl_table_is_registered_on_create_tables():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    assert "daily_pnl" in adapter.tables


def test_daily_pnl_has_expected_columns():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    table = adapter.tables["daily_pnl"]
    col_names = {c.name for c in table.columns}
    # Must match the fields petrosa-tradeengine's shared/mysql_client.py
    # sends on PUT /api/v1/mysql/daily_pnl (date, daily_pnl) plus the
    # server-injected audit columns (created_at, updated_at) and the
    # adapter-generated primary key (id).
    assert {"id", "date", "daily_pnl", "created_at", "updated_at"} <= col_names


def test_daily_pnl_date_column_has_unique_index():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    names = _index_names(adapter, "daily_pnl")
    assert "idx_daily_pnl_date" in names, (
        f"Expected idx_daily_pnl_date in daily_pnl indexes, got: {names}"
    )
    table = adapter.tables["daily_pnl"]
    idx = next(i for i in table.indexes if i.name == "idx_daily_pnl_date")
    assert idx.unique is True
    col_names = [c.name for c in idx.columns]
    assert col_names == ["date"]


def test_daily_pnl_has_a_recognized_time_column():
    # _time_column() (query_range/query_latest/get_record_count) requires
    # one of timestamp/entry_time/created_at — daily_pnl relies on
    # created_at, so this must not raise.
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    table = adapter.tables["daily_pnl"]
    col = adapter._time_column(table)
    assert col.name == "created_at"


def test_get_table_returns_daily_pnl_without_reflecting():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    table = adapter._get_table("daily_pnl")
    assert table.name == "daily_pnl"
