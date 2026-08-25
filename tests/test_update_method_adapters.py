"""
Coverage for MySQLAdapter.update() / MongoDBAdapter.update() (petrosa-data-manager#262).

These back the PUT /api/v1/{database}/{collection} route (generic.py::update_records)
and are what actually persist a change to an existing row/document — the route-level
tests in test_generic_update_records.py mock these methods; here we exercise the
real SQL UPDATE against an in-memory SQLite engine and the motor update_many() call
for MongoDB.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa

from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter


@pytest.fixture
def positions_adapter():
    """MySQLAdapter backed by SQLite with a positions-shaped table pre-seeded."""
    a = MySQLAdapter("sqlite:///:memory:")
    a.engine_options = {}
    a.engine = sa.create_engine("sqlite:///:memory:")
    a._connected = True
    a._create_tables()

    table = sa.Table(
        "positions",
        a.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    table.create(a.engine, checkfirst=True)
    a.tables["positions"] = table
    with a.engine.connect() as conn:
        conn.execute(
            table.insert(),
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "SHORT",
                    "quantity": 1.0,
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "quantity": 5.0,
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                },
            ],
        )
        conn.commit()
    return a


class TestMySQLAdapterUpdate:
    def test_update_existing_row_persists_and_returns_rowcount(self, positions_adapter):
        rowcount = positions_adapter.update(
            "positions",
            {"symbol": "BTCUSDT", "side": "SHORT"},
            {"quantity": 2.5},
        )
        assert rowcount == 1

        rows = positions_adapter.query_range("positions", datetime.min, datetime.max)
        btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
        assert btc["quantity"] == 2.5
        eth = next(r for r in rows if r["symbol"] == "ETHUSDT")
        assert eth["quantity"] == 5.0  # untouched

    def test_update_no_matching_rows_returns_zero(self, positions_adapter):
        rowcount = positions_adapter.update(
            "positions", {"symbol": "NOPE"}, {"quantity": 9.0}
        )
        assert rowcount == 0

    def test_update_empty_filter_refuses_full_table_update(self, positions_adapter):
        with pytest.raises(DatabaseError, match="would UPDATE every row") as exc_info:
            positions_adapter.update("positions", {}, {"quantity": 9.0})
        assert "would UPDATE every row" in str(exc_info.value)

    def test_update_filter_key_not_a_column_refuses(self, positions_adapter):
        with pytest.raises(DatabaseError, match="would UPDATE every row") as exc_info:
            positions_adapter.update(
                "positions", {"not_a_column": "x"}, {"quantity": 9.0}
            )
        assert "would UPDATE every row" in str(exc_info.value)

    def test_update_data_with_no_matching_columns_is_noop(self, positions_adapter):
        rowcount = positions_adapter.update(
            "positions",
            {"symbol": "BTCUSDT"},
            {"not_a_column": "x"},
        )
        assert rowcount == 0

    def test_update_raises_when_disconnected(self, positions_adapter):
        positions_adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            positions_adapter.update(
                "positions", {"symbol": "BTCUSDT"}, {"quantity": 1.0}
            )
        assert "Not connected" in str(exc_info.value)

    def test_update_isoformat_timestamp_string_is_coerced(self, positions_adapter):
        rowcount = positions_adapter.update(
            "positions",
            {"symbol": "ETHUSDT"},
            {"updated_at": "2026-08-25T00:00:00+00:00"},
        )
        assert rowcount == 1
        rows = positions_adapter.query_range("positions", datetime.min, datetime.max)
        eth = next(r for r in rows if r["symbol"] == "ETHUSDT")
        assert eth["updated_at"] is not None


class TestMongoDBAdapterUpdate:
    @pytest.fixture
    def adapter(self):
        a = MongoDBAdapter("mongodb://localhost:27017/test_db")
        a.client = MagicMock()
        a.db = MagicMock()
        a._connected = True
        return a

    @pytest.mark.asyncio
    async def test_update_calls_update_many_and_returns_modified_count(self, adapter):
        coll = MagicMock()
        coll.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
        adapter.db.__getitem__.return_value = coll

        result = await adapter.update("daily_pnl", {"symbol": "BTCUSDT"}, {"pnl": 42.0})

        assert result == 1
        coll.update_many.assert_called_once()
        call_args = coll.update_many.call_args
        assert call_args.args[0] == {"symbol": "BTCUSDT"}
        assert call_args.args[1] == {"$set": {"pnl": 42.0}}

    @pytest.mark.asyncio
    async def test_update_empty_filter_refuses_update_all(self, adapter):
        with pytest.raises(DatabaseError, match="update every document") as exc_info:
            await adapter.update("daily_pnl", {}, {"pnl": 1.0})
        assert "update every document" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_empty_data_is_noop(self, adapter):
        result = await adapter.update("daily_pnl", {"symbol": "BTCUSDT"}, {})
        assert result == 0

    @pytest.mark.asyncio
    async def test_update_rejects_dollar_key_filter_operator(self, adapter):
        with pytest.raises(DatabaseError, match="flat equality match") as exc_info:
            await adapter.update("daily_pnl", {"$where": "1==1"}, {"pnl": 1.0})
        assert "flat equality match" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_rejects_nested_operator_expression_filter(self, adapter):
        with pytest.raises(DatabaseError, match="flat equality match") as exc_info:
            await adapter.update("daily_pnl", {"pnl": {"$gte": 0}}, {"pnl": 1.0})
        assert "flat equality match" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_raises_when_disconnected(self, adapter):
        adapter._connected = False
        with pytest.raises(DatabaseError, match="Not connected") as exc_info:
            await adapter.update("daily_pnl", {"symbol": "BTCUSDT"}, {"pnl": 1.0})
        assert "Not connected" in str(exc_info.value)
