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


def _fake_write_engine(captured: dict):
    """A fake engine/connection that records the `records` payload passed
    to `conn.execute(stmt, records)` without needing real `INSERT IGNORE`
    support (SQLite's dialect doesn't have it, unlike MySQL, so the real
    sqlite_adapter engine can't run `write()`'s actual SQL — this fake lets
    tests isolate `write()`'s record-building logic from dialect-specific
    SQL execution)."""

    class FakeResult:
        rowcount = 1

    class FakeConn:
        def execute(self, stmt, records):
            captured["records"] = records
            return FakeResult()

        def begin(self):
            return self

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    fake_engine = MagicMock()
    fake_engine.connect.return_value = FakeConn()
    return fake_engine


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
            "cio_decisions",
            "execution_events",
            "pnl_events",
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

    def test_returns_cio_decisions_table_without_reflecting(self, sqlite_adapter):
        # 2026-09-20: cio_decisions is self-managed the same way daily_pnl
        # is — must not fall into the reflect-or-fail branch.
        table = sqlite_adapter._get_table("cio_decisions")
        assert table.name == "cio_decisions"
        assert "decision_id" in table.c
        assert "id" not in table.c  # decision_id IS the PK; no synthetic id

    def test_event_tables_have_natural_keys_and_indexes(self, sqlite_adapter):
        execution = sqlite_adapter._get_table("execution_events")
        pnl = sqlite_adapter._get_table("pnl_events")
        assert execution.primary_key.columns.keys() == ["event_key"]
        assert pnl.primary_key.columns.keys() == ["event_key"]
        assert execution.c.timestamp.type.fsp == 6
        assert pnl.c.timestamp.type.fsp == 6
        assert "payload" in execution.c
        assert "payload" in pnl.c
        assert any(
            index.name == "idx_execution_events_decision_timestamp"
            for index in execution.indexes
        )
        assert any(
            index.name == "idx_pnl_events_decision_timestamp" for index in pnl.indexes
        )

    def test_event_write_derives_idempotency_key(self, sqlite_adapter):
        from pydantic import BaseModel

        class EventRecord(BaseModel):
            decision_id: str
            strategy_id: str
            order_id: str
            event_type: str
            timestamp: datetime
            payload: dict = {}
            received_at: datetime

        captured: dict = {}
        event = EventRecord(
            decision_id="decision-1",
            strategy_id="strategy-1",
            order_id="order-1",
            event_type="filled",
            timestamp=datetime(2026, 9, 25, tzinfo=UTC),
            received_at=datetime(2026, 9, 25, tzinfo=UTC),
        )
        with patch.object(
            sqlite_adapter,
            "_ensure_connected",
            return_value=_fake_write_engine(captured),
        ):
            sqlite_adapter.write([event], "execution_events")
        assert captured["records"][0]["event_key"] == "order-1:filled"

    def test_pnl_event_write_derives_idempotency_key(self, sqlite_adapter):
        from pydantic import BaseModel

        class PnlRecord(BaseModel):
            decision_id: str
            strategy_id: str
            timestamp: datetime
            pnl_kind: str
            payload: dict = {}
            received_at: datetime

        captured: dict = {}
        timestamp = datetime(2026, 9, 25, 12, 0, 1, tzinfo=UTC)
        event = PnlRecord(
            decision_id="decision-1",
            strategy_id="strategy-1",
            timestamp=timestamp,
            pnl_kind="closed",
            received_at=timestamp,
        )
        with patch.object(
            sqlite_adapter,
            "_ensure_connected",
            return_value=_fake_write_engine(captured),
        ):
            sqlite_adapter.write([event], "pnl_events")
        assert captured["records"][0]["event_key"].startswith(
            "decision-1:closed:"
        )

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


class TestWriteIntegerAutoIncrementId:
    """2026-09-20: `write()` used to unconditionally synthesize a UUID
    string for any table with an `id` column, assuming a String PK (true
    for the manually-defined tables, which actually use `audit_id`/
    `metric_id`/etc. rather than a literal `id`). The reflected `signals`
    table (MySQL `petrosa_crypto.signals`) has a real `id int(11)
    auto_increment` PK — injecting a UUID string there would violate the
    column type. `write()` must leave integer auto-increment `id` columns
    alone and let the database assign them."""

    @pytest.fixture
    def signals_like_adapter(self, sqlite_adapter):
        """Register a table shaped like the real MySQL `signals` table:
        `id int auto_increment` PK, not a UUID string."""
        table = sa.Table(
            "signals",
            sqlite_adapter.metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("symbol", sa.String(20), nullable=False),
            sa.Column("signal_type", sa.String(10), nullable=False),
            sa.Column("confidence", sa.Float, nullable=False),
            sa.Column("strategy", sa.String(50), nullable=False),
            sa.Column("timestamp", sa.DateTime, nullable=False),
        )
        table.create(sqlite_adapter.engine, checkfirst=True)
        sqlite_adapter.tables["signals"] = table
        return sqlite_adapter

    def test_write_does_not_inject_uuid_into_integer_id_column(
        self, signals_like_adapter
    ):
        from pydantic import BaseModel, ConfigDict

        class SignalRecord(BaseModel):
            model_config = ConfigDict(extra="allow")
            symbol: str
            signal_type: str
            confidence: float
            strategy: str
            timestamp: datetime

        captured: dict = {}
        with patch.object(
            signals_like_adapter,
            "_ensure_connected",
            return_value=_fake_write_engine(captured),
        ):
            result = signals_like_adapter.write(
                [
                    SignalRecord(
                        symbol="BTCUSDT",
                        signal_type="buy",
                        confidence=0.9,
                        strategy="ema_pullback_continuation",
                        timestamp=datetime(2026, 9, 20, tzinfo=UTC),
                    )
                ],
                "signals",
            )

        assert result.inserted == 1
        # The auto-increment column must never receive an injected value —
        # a UUID string would violate the integer column type.
        assert "id" not in captured["records"][0]

    def test_write_still_injects_uuid_for_string_id_tables(self, sqlite_adapter):
        """Regression guard: tables with a genuine String `id` PK must keep
        getting an auto-generated UUID when none is supplied."""
        table = sa.Table(
            "widgets",
            sqlite_adapter.metadata,
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(50), nullable=False),
        )
        table.create(sqlite_adapter.engine, checkfirst=True)
        sqlite_adapter.tables["widgets"] = table

        from pydantic import BaseModel, ConfigDict

        class WidgetRecord(BaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        captured: dict = {}
        with patch.object(
            sqlite_adapter,
            "_ensure_connected",
            return_value=_fake_write_engine(captured),
        ):
            sqlite_adapter.write([WidgetRecord(name="thing")], "widgets")

        record = captured["records"][0]
        assert isinstance(record["id"], str)
        assert len(record["id"]) == 36  # uuid4 string length


class TestCioDecisionsTable:
    """2026-09-20: `cio_decisions` is the permanent MySQL historic copy of
    the Mongo collection (which was cut to a 1-day TTL). Dual-written by
    `decision_consumer.py._persist`."""

    def test_write_stores_decision_id_pk_and_json_columns(self, sqlite_adapter):
        from pydantic import BaseModel, ConfigDict

        class DecisionRecord(BaseModel):
            model_config = ConfigDict(extra="allow")
            decision_id: str
            strategy_id: str
            timestamp: datetime
            symbol: str | None = None
            action: str | None = None
            price: float | None = None
            quantity: float | None = None
            confidence: float | None = None
            source: str | None = None
            reasoning: dict
            subject: str | None = None
            payload: dict
            received_at: datetime

        captured: dict = {}
        with patch.object(
            sqlite_adapter,
            "_ensure_connected",
            return_value=_fake_write_engine(captured),
        ):
            result = sqlite_adapter.write(
                [
                    DecisionRecord(
                        decision_id="dec_20260920T120000000_abc123",
                        strategy_id="strat_momentum_v1",
                        timestamp=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                        symbol="BTCUSDT",
                        action="buy",
                        price=65000.0,
                        quantity=0.001,
                        confidence=0.9,
                        source="petrosa-cio",
                        reasoning={"cio_justification": "Momentum confluence"},
                        subject="signals.trading.strat_momentum_v1",
                        payload={"current_price": 65000.0},
                        received_at=datetime(2026, 9, 20, 12, 0, 1, tzinfo=UTC),
                    )
                ],
                "cio_decisions",
            )

        assert result.inserted == 1
        record = captured["records"][0]
        # decision_id IS the PK — no synthetic id column exists on this table.
        assert record["decision_id"] == "dec_20260920T120000000_abc123"
        assert "id" not in record
        assert record["reasoning"] == {"cio_justification": "Momentum confluence"}
        assert record["payload"] == {"current_price": 65000.0}


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

    def test_query_range_enforces_db_side_limit(self, sqlite_adapter):
        # petrosa-data-manager#331: query_range must cap rows at the SQL
        # level (LIMIT), not return everything for the caller to slice.
        table = sqlite_adapter._get_table("audit_logs")
        with sqlite_adapter.engine.connect() as conn:
            conn.execute(
                table.insert(),
                [
                    {
                        "audit_id": f"a{i}",
                        "dataset_id": "d1",
                        "symbol": "BTCUSDT",
                        "audit_type": "x",
                        "timestamp": datetime(2026, 1, 1, i, tzinfo=UTC),
                    }
                    for i in range(5)
                ],
            )
            conn.commit()

        rows = sqlite_adapter.query_range(
            "audit_logs",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "BTCUSDT",
            limit=2,
        )
        # A DB-side LIMIT returns exactly 2 rows, not all 5 sliced in Python.
        assert len(rows) == 2
        assert [r["audit_id"] for r in rows] == ["a0", "a1"]

    def test_query_range_descending_limit_returns_newest_first(self, sqlite_adapter):
        # petrosa-data-manager#331 correctness warning: LIMIT must be applied
        # AFTER an ORDER BY that matches the caller's requested direction, or
        # a `sort_order="desc"` caller silently gets the oldest rows instead
        # of the newest.
        table = sqlite_adapter._get_table("audit_logs")
        with sqlite_adapter.engine.connect() as conn:
            conn.execute(
                table.insert(),
                [
                    {
                        "audit_id": f"a{i}",
                        "dataset_id": "d1",
                        "symbol": "BTCUSDT",
                        "audit_type": "x",
                        "timestamp": datetime(2026, 1, 1, i, tzinfo=UTC),
                    }
                    for i in range(5)
                ],
            )
            conn.commit()

        rows = sqlite_adapter.query_range(
            "audit_logs",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "BTCUSDT",
            limit=2,
            descending=True,
        )
        assert [r["audit_id"] for r in rows] == ["a4", "a3"]

    def test_query_range_offset_skips_rows_at_db_level(self, sqlite_adapter):
        table = sqlite_adapter._get_table("audit_logs")
        with sqlite_adapter.engine.connect() as conn:
            conn.execute(
                table.insert(),
                [
                    {
                        "audit_id": f"a{i}",
                        "dataset_id": "d1",
                        "symbol": "BTCUSDT",
                        "audit_type": "x",
                        "timestamp": datetime(2026, 1, 1, i, tzinfo=UTC),
                    }
                    for i in range(5)
                ],
            )
            conn.commit()

        rows = sqlite_adapter.query_range(
            "audit_logs",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "BTCUSDT",
            limit=2,
            offset=2,
        )
        assert [r["audit_id"] for r in rows] == ["a2", "a3"]

    def test_query_range_without_limit_returns_full_range(self, sqlite_adapter):
        # Backward compatibility: callers that don't pass `limit` (analytics
        # calculators, the cutover fallback path) must still get everything.
        table = sqlite_adapter._get_table("audit_logs")
        with sqlite_adapter.engine.connect() as conn:
            conn.execute(
                table.insert(),
                [
                    {
                        "audit_id": f"a{i}",
                        "dataset_id": "d1",
                        "symbol": "BTCUSDT",
                        "audit_type": "x",
                        "timestamp": datetime(2026, 1, 1, i, tzinfo=UTC),
                    }
                    for i in range(5)
                ],
            )
            conn.commit()

        rows = sqlite_adapter.query_range(
            "audit_logs",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "BTCUSDT",
        )
        assert len(rows) == 5

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
