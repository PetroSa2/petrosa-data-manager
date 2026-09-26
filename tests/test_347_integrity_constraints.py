"""Offline acceptance tests for data-manager#347."""

from pathlib import Path

from data_manager.db.mysql_adapter import MySQLAdapter

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"


def test_mysql_session_mode_is_mysql_only_and_pool_budget_is_unchanged():
    mysql = MySQLAdapter("mysql+pymysql://u:p@h:3306/db")
    args = mysql.engine_options["connect_args"]
    assert all(
        flag in args["init_command"]
        for flag in ("STRICT_TRANS_TABLES", "NO_ZERO_DATE", "NO_ZERO_IN_DATE")
    )
    assert mysql.engine_options["pool_recycle"] == 10
    assert mysql.engine_options["pool_size"] == 5
    assert mysql.engine_options["max_overflow"] == 7
    assert mysql.engine_options["pool_pre_ping"] is True


def test_sqlite_does_not_receive_init_command_and_can_create_tables():
    sqlite = MySQLAdapter("sqlite:///:memory:")
    assert "init_command" not in sqlite.engine_options["connect_args"]
    sqlite._create_tables()


def test_migration_contract():
    dedupe = (MIGRATIONS / "008_klines_m1_m15_dedupe.sql").read_text()
    unique = (MIGRATIONS / "008_klines_m1_m15_unique.sql").read_text()
    rollback = (MIGRATIONS / "008_rollback_klines_m1_m15_unique.sql").read_text()
    assert "LIMIT 1000" in dedupe
    assert "symbol = ''" in dedupe and "timestamp = '0000-00-00 00:00:00'" in dedupe
    assert "uniq_klines_m1_symbol_timestamp" in unique
    assert "uniq_klines_m15_symbol_timestamp" in unique
    assert "COUNT(*) - COUNT(DISTINCT symbol, timestamp)" in unique
    assert "ALGORITHM=INPLACE, LOCK=NONE" in unique
    assert "ALGORITHM=INPLACE, LOCK=NONE" in rollback
    assert "IF EXISTS" not in unique + rollback
