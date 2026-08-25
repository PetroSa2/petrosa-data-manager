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
            "daily_pnl",
        ):
            assert table_name in a.tables


class TestGetTable:
    def test_returns_pre_registered_table(self, sqlite_adapter):
        table = sqlite_adapter._get_table("datasets")
        assert table.name == "datasets"

    def test_returns_daily_pnl_table_without_reflecting(self, sqlite_adapter):
        # petrosa-data-manager#264: daily_pnl is self-managed (registered in
        # _create_tables()), so _get_table() must return it directly instead
        # of falling into the reflect-or-fail branch that used to raise
        # NoSuchTableError / "Unknown collection or failed to reflect".
        table = sqlite_adapter._get_table("daily_pnl")
        assert table.name == "daily_pnl"

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


class TestTimeColumnFallback:
    """#548-adjacent (petrosa-tradeengine) discovery: query_range/query_latest/
    get_record_count hardcoded ``table.c.timestamp`` for every collection, but
    the real ``positions`` table (petrosa_k8s/k8s/tradeengine/mysql-schema-job.yaml)
    has no ``timestamp`` column — only ``entry_time`` — so every call against
    it raised ``KeyError: 'timestamp'`` (100% reproducible, ~7.8/min in prod).
    """

    @pytest.fixture
    def positions_like_adapter(self, sqlite_adapter):
        """Register a table shaped like the real ``positions`` table: has
        ``entry_time`` but no ``timestamp`` column."""
        table = sa.Table(
            "positions",
            sqlite_adapter.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("symbol", sa.String(20), nullable=False),
            sa.Column("entry_time", sa.DateTime, nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
        table.create(sqlite_adapter.engine, checkfirst=True)
        sqlite_adapter.tables["positions"] = table
        with sqlite_adapter.engine.connect() as conn:
            conn.execute(
                table.insert(),
                [
                    {
                        "symbol": "BTCUSDT",
                        "entry_time": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                        "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                    }
                ],
            )
            conn.commit()
        return sqlite_adapter

    def test_query_range_falls_back_to_entry_time(self, positions_like_adapter):
        rows = positions_like_adapter.query_range(
            "positions",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTCUSDT"

    def test_query_range_excludes_out_of_range_entry_time(self, positions_like_adapter):
        rows = positions_like_adapter.query_range(
            "positions",
            datetime(2026, 2, 1, tzinfo=UTC),
            datetime(2026, 2, 2, tzinfo=UTC),
        )
        assert rows == []

    def test_query_latest_falls_back_to_entry_time(self, positions_like_adapter):
        rows = positions_like_adapter.query_latest("positions", limit=1)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTCUSDT"

    def test_get_record_count_falls_back_to_entry_time(self, positions_like_adapter):
        count = positions_like_adapter.get_record_count(
            "positions",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert count == 1

    def test_time_column_prefers_timestamp_when_present(self, sqlite_adapter):
        table = sqlite_adapter._get_table("audit_logs")
        assert sqlite_adapter._time_column(table) is table.c.timestamp

    def test_time_column_raises_for_table_with_no_known_time_column(
        self, sqlite_adapter
    ):
        table = sa.Table(
            "mystery_table",
            sqlite_adapter.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("symbol", sa.String(20)),
        )
        table.create(sqlite_adapter.engine, checkfirst=True)
        with pytest.raises(
            DatabaseError, match="no recognized time column"
        ) as exc_info:
            sqlite_adapter._time_column(table)
        assert "mystery_table" in str(exc_info.value)


class TestEnsureConnected:
    def test_raises_when_no_engine(self):
        a = MySQLAdapter("mysql://x")
        a.engine = None
        with pytest.raises(DatabaseError) as exc_info:
            a._ensure_connected()
        assert exc_info.value is not None
