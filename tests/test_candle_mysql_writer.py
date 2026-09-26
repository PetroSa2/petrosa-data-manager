from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects.mysql import (
    dialect as mysql_dialect,
    insert,
)
from sqlalchemy.sql.sqltypes import DateTime, String

from data_manager.backfiller.orchestrator import BackfillOrchestrator
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.db.repositories.candle_repository import (
    candle_to_mysql_kline,
)
from data_manager.models.market_data import Candle, MySQLKlineRow


def make_candle(timeframe: str = "1h", **overrides) -> Candle:
    values = {
        "symbol": "BTCUSDT",
        "timestamp": datetime(2026, 9, 24, 2, 0, tzinfo=UTC),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": Decimal("105"),
        "volume": Decimal("1000"),
        "quote_volume": Decimal("105000"),
        "trades_count": 42,
        "timeframe": timeframe,
    }
    values.update(overrides)
    return Candle(**values)


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_mysql_row_has_exact_klines_column_parity(timeframe):
    adapter = MySQLAdapter("sqlite:///:memory:")
    table = adapter._create_klines_table(timeframe)

    assert set(candle_to_mysql_kline(make_candle(timeframe)).model_dump()) == set(
        table.c.keys()
    )


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_mysql_row_is_complete_for_strict_mode(timeframe):
    adapter = MySQLAdapter("sqlite:///:memory:")
    table = adapter._create_klines_table(timeframe)
    row = candle_to_mysql_kline(make_candle(timeframe)).model_dump()

    for column in table.c:
        value = row[column.name]
        if not column.nullable:
            assert value is not None
        if isinstance(column.type, DateTime):
            assert value.tzinfo is None
            assert value.year >= 2000
        if isinstance(column.type, String):
            assert 0 < len(value) <= column.type.length
    assert row["open_price"] != 0
    assert row["high_price"] != 0
    assert row["low_price"] != 0
    assert row["close_price"] != 0


def test_compiled_mysql_insert_contains_complete_row_columns():
    adapter = MySQLAdapter("sqlite:///:memory:")
    table = adapter._create_klines_table("15m")
    candle = make_candle("15m")
    row = candle_to_mysql_kline(candle)
    insert_statement = insert(table)
    statement = insert_statement.on_duplicate_key_update(
        extracted_at=insert_statement.inserted.extracted_at
    )

    compiled = statement.compile(
        dialect=mysql_dialect(), column_keys=list(row.model_dump())
    ).string
    for column in (
        "open_time",
        "close_time",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "interval",
        "quote_asset_volume",
        "number_of_trades",
        "extracted_at",
        "extractor_version",
        "source",
    ):
        assert column in compiled

    raw_compiled = statement.compile(
        dialect=mysql_dialect(), column_keys=list(candle.model_dump())
    ).string
    assert "open_price" not in raw_compiled
    assert "open_time" not in raw_compiled


@pytest.mark.parametrize(
    ("timeframe", "minutes"),
    [("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440)],
)
def test_mysql_row_derives_times_and_deterministic_id(timeframe, minutes):
    aware = make_candle(timeframe)
    naive = make_candle(timeframe, timestamp=aware.timestamp.replace(tzinfo=None))

    row = candle_to_mysql_kline(aware)
    naive_row = candle_to_mysql_kline(naive)
    expected_time = datetime(2026, 9, 24, 2, 0)

    assert row.open_time == expected_time
    assert row.timestamp == expected_time
    assert row.close_time - row.open_time == timedelta(minutes=minutes)
    assert row.id == "BTCUSDT_1790215200000"
    assert naive_row.id == row.id
    assert naive_row.open_time == row.open_time


def test_mysql_row_derives_optional_values_and_preserves_native_types():
    row = candle_to_mysql_kline(
        make_candle(
            quote_volume=None,
            trades_count=None,
            taker_buy_base_volume=Decimal("12.5"),
            taker_buy_quote_volume=Decimal("13.5"),
        ),
        extracted_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
    )

    assert row.price_change == Decimal("5")
    assert row.price_change_percent == Decimal("5.0000")
    assert row.quote_asset_volume == Decimal("0")
    assert row.number_of_trades == 0
    assert row.taker_buy_base_asset_volume == Decimal("12.5")
    assert row.taker_buy_quote_asset_volume == Decimal("13.5")
    assert isinstance(row.timestamp, datetime)
    assert isinstance(row.open_price, Decimal)
    assert row.extracted_at == datetime(2026, 9, 25, 12, 0)

    zero_open = candle_to_mysql_kline(make_candle(open=Decimal("0")))
    assert zero_open.price_change_percent == Decimal("0")


def test_mysql_row_rejects_invalid_timeframe():
    with pytest.raises(ValueError):
        candle_to_mysql_kline(make_candle("xyz"))


def test_candle_dump_shape_excludes_taker_buy_fields():
    candle = make_candle(
        taker_buy_base_volume=Decimal("12.5"),
        taker_buy_quote_volume=Decimal("13.5"),
    )

    assert set(candle.model_dump()) == {
        "symbol",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades_count",
        "timeframe",
    }


@pytest.mark.asyncio
async def test_backfill_populates_aware_timestamp_and_taker_volumes():
    db_manager = MagicMock()
    orchestrator = BackfillOrchestrator(db_manager)
    orchestrator.binance_client.get_klines = AsyncMock(
        return_value=[
            [
                1790215200000,
                "100",
                "110",
                "90",
                "105",
                "1000",
                1790215259999,
                "105000",
                42,
                "12.5",
                "13.5",
                "0",
            ]
        ]
    )
    orchestrator.candle_repo.insert_batch = AsyncMock(return_value=1)

    await orchestrator._backfill_candles(
        "job-1",
        "BTCUSDT",
        "15m",
        datetime(2026, 9, 24, 2, 0, tzinfo=UTC),
        datetime(2026, 9, 24, 2, 1, tzinfo=UTC),
    )

    candle = orchestrator.candle_repo.insert_batch.call_args[0][0][0]
    assert candle.timestamp == datetime(2026, 9, 24, 2, 0, tzinfo=UTC)
    assert candle.taker_buy_base_volume == Decimal("12.5")
    assert candle.taker_buy_quote_volume == Decimal("13.5")
    assert isinstance(candle_to_mysql_kline(candle), MySQLKlineRow)
