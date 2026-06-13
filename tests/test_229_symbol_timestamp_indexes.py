"""Tests for data-manager#229 — (symbol, timestamp) composite indexes on audit_logs + health_metrics."""

from data_manager.db.mysql_adapter import MySQLAdapter


def _index_names(adapter, table_name):
    table = adapter.tables[table_name]
    return {idx.name for idx in table.indexes}


def test_audit_logs_has_symbol_timestamp_composite_index():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    names = _index_names(adapter, "audit_logs")
    assert "idx_audit_logs_symbol_timestamp" in names, (
        f"Expected idx_audit_logs_symbol_timestamp in audit_logs indexes, got: {names}"
    )


def test_health_metrics_has_symbol_timestamp_composite_index():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    names = _index_names(adapter, "health_metrics")
    assert "idx_health_metrics_symbol_timestamp" in names, (
        f"Expected idx_health_metrics_symbol_timestamp in health_metrics indexes, got: {names}"
    )


def test_audit_logs_composite_index_columns():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    table = adapter.tables["audit_logs"]
    idx = next(i for i in table.indexes if i.name == "idx_audit_logs_symbol_timestamp")
    col_names = [c.name for c in idx.columns]
    assert col_names == ["symbol", "timestamp"], (
        f"Expected ['symbol', 'timestamp'], got {col_names}"
    )


def test_health_metrics_composite_index_columns():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter._create_tables()
    table = adapter.tables["health_metrics"]
    idx = next(
        i for i in table.indexes if i.name == "idx_health_metrics_symbol_timestamp"
    )
    col_names = [c.name for c in idx.columns]
    assert col_names == ["symbol", "timestamp"], (
        f"Expected ['symbol', 'timestamp'], got {col_names}"
    )


def test_migration_sql_file_exists():
    import os

    migration_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "data_manager",
        "scripts",
        "migrations",
        "006_symbol_timestamp_composites.sql",
    )
    assert os.path.isfile(migration_path), (
        f"Migration file 006_symbol_timestamp_composites.sql not found at {migration_path}"
    )
