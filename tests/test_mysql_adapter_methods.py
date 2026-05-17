"""
Coverage for MySQLAdapter methods. Uses an in-memory SQLite engine for actual
table creation + queries (covers the real _create_tables and _get_table paths),
falling back to mocks for connect/disconnect edge cases.
"""

from datetime import UTC, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mysql_adapter import MySQLAdapter


@pytest.fixture
def sqlite_adapter():
    """Build an adapter backed by an in-memory SQLite engine."""
    a = MySQLAdapter("sqlite:///:memory:")
    # Replace engine_options with SQLite-compatible ones.
    a.engine_options = {}
    a.engine = sa.create_engine("sqlite:///:memory:")
    a._connected = True
    a._create_tables()
    return a


class TestBuildConnectionString:
    def test_falls_back_to_defaults_when_no_constants(self):
        with patch("data_manager.db.mysql_adapter.constants") as const:
            # Configure constants to have nothing (so hasattr returns False).
            # Use spec=[] for empty hasattr surface.
            const.spec = []
            for attr in (
                "MYSQL_USER",
                "MYSQL_PASSWORD",
                "MYSQL_HOST",
                "MYSQL_PORT",
                "MYSQL_DB",
            ):
                if hasattr(const, attr):
                    delattr(const, attr)
            # Need to call the build method directly.
            a = MySQLAdapter("mysql://user:pass@host:3306/db")
            result = a._build_connection_string()
            # When constants are absent, defaults are used.
            assert "mysql+pymysql://" in result

    def test_uses_constants_when_present(self):
        with patch("data_manager.db.mysql_adapter.constants") as const:
            const.MYSQL_USER = "admin"
            const.MYSQL_PASSWORD = "secret"
            const.MYSQL_HOST = "db.example.com"
            const.MYSQL_PORT = 13306
            const.MYSQL_DB = "petrosa"
            a = MySQLAdapter("mysql://x")
            result = a._build_connection_string()
            assert result == "mysql+pymysql://admin:secret@db.example.com:13306/petrosa"


class TestConnect:
    def test_connect_wraps_sqlalchemy_error(self):
        from sqlalchemy.exc import SQLAlchemyError

        with patch("data_manager.db.mysql_adapter.create_engine") as ce:
            ce.side_effect = SQLAlchemyError("conn refused")
            a = MySQLAdapter("mysql://x:y@h:3306/db")
            with pytest.raises(DatabaseError, match="Failed to connect") as exc_info:
                a.connect()
            assert "Failed to connect" in str(exc_info.value)

    def test_disconnect_calls_dispose(self):
        a = MySQLAdapter("mysql://x")
        a.engine = MagicMock()
        a._connected = True
        a.disconnect()
        assert a._connected is False
        a.engine.dispose.assert_called_once()

    def test_disconnect_no_engine_is_safe(self):
        a = MySQLAdapter("mysql://x")
        a.engine = None
        # Must not raise.
        a.disconnect()
        assert a._connected is False


class TestCreateTablesViaConnect:
    def test_connect_creates_tables_in_sqlite(self):
        a = MySQLAdapter("sqlite:///:memory:")
        a.engine_options = {}
        # Hand-roll connect: SQLAlchemy SELECT 1 works on SQLite too.
        a.engine = sa.create_engine("sqlite:///:memory:")
        a._connected = True
        a._create_tables()
        # All documented tables should be registered.
        for table_name in (
            "datasets",
            "audit_logs",
            "health_metrics",
            "backfill_jobs",
            "lineage_records",
            "schemas",
        ):
            assert table_name in a.tables


class TestGetTable:
    def test_returns_pre_registered_table(self, sqlite_adapter):
        table = sqlite_adapter._get_table("datasets")
        assert table.name == "datasets"

    def test_creates_klines_table_from_binance_interval(self, sqlite_adapter):
        # klines_15m → physical klines_m15
        table = sqlite_adapter._get_table("klines_15m")
        assert table is not None

    def test_creates_klines_table_from_financial_suffix(self, sqlite_adapter):
        # klines_h1 → financial style is already correct
        table = sqlite_adapter._get_table("klines_h1")
        assert table is not None

    def test_creates_klines_table_for_day_interval(self, sqlite_adapter):
        table = sqlite_adapter._get_table("klines_d1")
        assert table is not None


class TestDisconnectedGuards:
    """Verify the not-connected guards on all the I/O methods."""

    def test_write_raises_when_disconnected(self, sqlite_adapter):
        sqlite_adapter._connected = False
        from pydantic import BaseModel

        class Rec(BaseModel):
            x: str = "y"

        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            sqlite_adapter.write([Rec()], "audit_logs")
        assert "Not connected" in str(exc_info.value)

    def test_query_range_raises_when_disconnected(self, sqlite_adapter):
        sqlite_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            sqlite_adapter.query_range(
                "audit_logs",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )
        assert "Not connected" in str(exc_info.value)

    def test_query_latest_raises_when_disconnected(self, sqlite_adapter):
        sqlite_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            sqlite_adapter.query_latest("audit_logs")
        assert "Not connected" in str(exc_info.value)

    def test_get_record_count_raises_when_disconnected(self, sqlite_adapter):
        sqlite_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            sqlite_adapter.get_record_count("audit_logs")
        assert "Not connected" in str(exc_info.value)

    def test_delete_range_raises_when_disconnected(self, sqlite_adapter):
        sqlite_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            sqlite_adapter.delete_range(
                "audit_logs",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            )
        assert "Not connected" in str(exc_info.value)

    def test_write_empty_list_returns_zero(self, sqlite_adapter):
        assert sqlite_adapter.write([], "audit_logs") == 0

    def test_ensure_indexes_is_noop(self, sqlite_adapter):
        # ensure_indexes is documented as noop for MySQL (handled during table create).
        result = sqlite_adapter.ensure_indexes("audit_logs")
        # Returns None; assert that's what we get.
        assert result is None


class TestEnsureConnected:
    def test_raises_when_no_engine(self):
        a = MySQLAdapter("mysql://x")
        a.engine = None
        with pytest.raises(DatabaseError) as exc_info:
            a._ensure_connected()
        assert exc_info.value is not None
