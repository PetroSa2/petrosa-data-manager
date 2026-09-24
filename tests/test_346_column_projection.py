from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

import data_manager.api.app as api_module
from data_manager.api.routes.generic import _execute_query_internal
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.candle_repository import (
    _MYSQL_COLUMN_MAP,
    MYSQL_CANDLE_COLUMNS,
    CandleRepository,
    map_mysql_row,
)
from data_manager.maintenance.candle_warmup_backfill import row_to_candle


@pytest.fixture
def sqlite_adapter():
    adapter = MySQLAdapter("sqlite:///:memory:")
    adapter.engine_options = {}
    adapter.engine = sa.create_engine("sqlite:///:memory:")
    adapter._connected = True
    return adapter


def test_projection_resolves_columns_and_preserves_whole_table_fallback(
    sqlite_adapter, caplog
):
    table = sqlite_adapter._get_table("klines_1h")
    dialect = mysql.dialect()

    full = str(
        sqlite_adapter._select_table_columns(table, None).compile(dialect=dialect)
    )
    projected = str(
        sqlite_adapter._select_table_columns(table, ["open_price", "bogus"]).compile(
            dialect=dialect
        )
    )
    assert "open_price" in projected
    assert "bogus" not in projected
    assert "open_time" in full
    assert "open_time" not in projected

    with caplog.at_level("WARNING"):
        fallback = sqlite_adapter._select_table_columns(table, ["nope"])
    assert str(fallback.compile(dialect=dialect)) == full
    assert "No requested columns exist" in caplog.text

    empty = sqlite_adapter._select_table_columns(table, [])
    assert str(empty.compile(dialect=dialect)) == full


def test_find_paginated_projects_rows_but_counts_full_table(sqlite_adapter):
    table = sqlite_adapter._get_table("klines_1h")
    with sqlite_adapter.engine.begin() as connection:
        connection.execute(
            table.insert(),
            {
                "id": "row-1",
                "symbol": "BTCUSDT",
                "timestamp": datetime(2026, 1, 1),
                "open_time": datetime(2026, 1, 1),
                "close_time": datetime(2026, 1, 1),
                "interval": "1h",
                "open_price": 1,
                "high_price": 2,
                "low_price": 0,
                "close_price": 1,
                "volume": 10,
                "quote_asset_volume": 11,
                "number_of_trades": 12,
                "taker_buy_base_asset_volume": 5,
                "taker_buy_quote_asset_volume": 6,
                "price_change": 0,
                "price_change_percent": 0,
                "extracted_at": datetime(2026, 1, 1),
                "extractor_version": "test",
                "source": "test",
            },
        )

    rows, total = sqlite_adapter.find_paginated(
        "klines_1h", columns=["symbol", "bogus"]
    )
    assert rows == [{"symbol": "BTCUSDT"}]
    assert total == 1


@pytest.mark.asyncio
async def test_mysql_candle_reads_use_derived_projection_at_all_paths():
    mysql = Mock()
    mysql.query_range = Mock(return_value=[])
    mysql.query_latest = Mock(return_value=[])
    repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=None)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)

    with patch(
        "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
        "mysql",
    ):
        await repo.get_range("BTCUSDT", "1h", start, end)
        await repo.get_latest("BTCUSDT", "1h", limit=2)

    assert set(MYSQL_CANDLE_COLUMNS) == set(_MYSQL_COLUMN_MAP.values())
    assert mysql.query_range.call_args.kwargs["columns"] == MYSQL_CANDLE_COLUMNS
    assert mysql.query_latest.call_args.kwargs["columns"] == MYSQL_CANDLE_COLUMNS


@pytest.mark.asyncio
async def test_mysql_fallback_reads_use_projection():
    mysql = Mock()
    mysql.query_range = Mock(return_value=[])
    mysql.query_latest = Mock(return_value=[])
    repo = CandleRepository(mysql_adapter=mysql, mongodb_adapter=AsyncMock())

    with (
        patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_DATABASE_TYPE",
            "mongodb",
        ),
        patch(
            "data_manager.db.repositories.candle_repository.constants.CANDLE_READ_FALLBACK_ENABLED",
            True,
        ),
    ):
        await repo._read_fallback_range(
            "BTCUSDT", "1h", datetime(2026, 1, 1), datetime(2026, 1, 2)
        )
        await repo._read_fallback_latest("BTCUSDT", "1h", 2)

    assert mysql.query_range.call_args.kwargs["columns"] == MYSQL_CANDLE_COLUMNS
    assert mysql.query_latest.call_args.kwargs["columns"] == MYSQL_CANDLE_COLUMNS


def test_map_mysql_row_and_warmup_projection_contract():
    row = {
        "open_price": 1,
        "high_price": 2,
        "low_price": 0,
        "close_price": 1,
        "volume": 10,
        "timestamp": datetime(2026, 1, 1),
        "symbol": "BTCUSDT",
        "interval": "1h",
        "open_time": datetime(2026, 1, 1),
        "quote_asset_volume": 11,
        "number_of_trades": 12,
    }
    mapped = map_mysql_row(row)
    assert set(mapped) == set(_MYSQL_COLUMN_MAP)
    candle = row_to_candle(row, "1h")
    assert candle is not None
    assert candle.quote_volume == 11
    assert candle.trades_count == 12


@pytest.mark.asyncio
async def test_generic_mysql_query_pushes_field_list_down():
    adapter = Mock()
    adapter.find_paginated.return_value = ([{"symbol": "BTCUSDT", "volume": 10}], 1)
    manager = SimpleNamespace(
        mysql_adapter=adapter,
        increment_query_count=Mock(),
    )
    with patch.object(api_module, "db_manager", manager):
        response = await _execute_query_internal(
            "mysql", "klines_1h", None, None, 10, 0, ["symbol"]
        )

    assert adapter.find_paginated.call_args.kwargs["columns"] == ["symbol"]
    assert response["data"] == [{"symbol": "BTCUSDT"}]
