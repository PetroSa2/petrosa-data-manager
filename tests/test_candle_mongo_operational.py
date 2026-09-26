from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

import constants
from data_manager.db.repositories.candle_repository import (
    CandleRepository,
    candle_to_mongo_kline,
    map_mongo_kline_doc,
)
from data_manager.models.market_data import Candle

NOW = datetime(2026, 9, 25, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mongo_is_primary(monkeypatch):
    monkeypatch.setattr(constants, "CANDLE_DATABASE_TYPE", "mongodb")


def live_doc(**overrides):
    document = {
        "timestamp": NOW,
        "open_time": "2026-09-25T15:00:00",
        "close_time": "2026-09-25T16:00:00",
        "open_price": "83973.50",
        "high_price": "84059.40",
        "low_price": "83711.00",
        "close_price": "83823.70",
        "volume": "12.5",
        "quote_asset_volume": "1040000",
        "number_of_trades": 42,
        "symbol": "BTCUSDT",
        "interval": "1h",
    }
    document.update(overrides)
    return document


def candle() -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timestamp=NOW,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("12.5"),
        timeframe="1h",
    )


def repository(mongo) -> CandleRepository:
    return CandleRepository(mysql_adapter=Mock(), mongodb_adapter=mongo)


def test_maps_extractor_document_to_canonical_shape():
    mapped = map_mongo_kline_doc(live_doc())

    assert mapped is not None
    assert mapped["open"] == Decimal("83973.50")
    assert mapped["high"] == Decimal("84059.40")
    assert mapped["low"] == Decimal("83711.00")
    assert mapped["close"] == Decimal("83823.70")
    assert mapped["timeframe"] == "1h"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"open_price": "0"}, "zero_ohlc"),
        ({"close_price": "0"}, "zero_ohlc"),
        ({"high_price": "not-a-number"}, "non_numeric_ohlc"),
        ({"low_price": None}, "missing_ohlc"),
    ],
)
def test_invalid_extractor_documents_are_dropped(override, reason):
    mapped = map_mongo_kline_doc(live_doc(**override))

    assert mapped is None


@pytest.mark.asyncio
async def test_mongo_latest_reads_extractor_collection_and_filters_invalid_docs():
    mongo = Mock()
    mongo.query_latest = AsyncMock(return_value=[live_doc(), live_doc(open_price="0")])

    result = await repository(mongo).get_latest("BTCUSDT", "1h", 5)

    assert len(result) == 1
    mongo.query_latest.assert_awaited_once_with("klines_1h", "BTCUSDT", 5)


@pytest.mark.asyncio
async def test_mongo_range_reads_extractor_collection_and_maps_docs():
    mongo = Mock()
    mongo.query_range = AsyncMock(return_value=[live_doc()])
    start = NOW - timedelta(hours=1)

    result = await repository(mongo).get_range(
        "BTCUSDT", "1h", start, NOW, limit=5, descending=True
    )

    assert result[0]["close"] == Decimal("83823.70")
    mongo.query_range.assert_awaited_once_with(
        "klines_1h",
        start,
        NOW,
        "BTCUSDT",
        limit=5,
        offset=0,
        descending=True,
    )


@pytest.mark.asyncio
async def test_mongo_insert_batch_writes_extractor_schema():
    mongo = Mock()
    mongo.write = AsyncMock(return_value=1)

    result = await repository(mongo).insert_batch([candle()])

    assert result == 1
    written, collection = mongo.write.await_args.args
    assert collection == "klines_1h"
    document = written[0].model_dump()
    assert document["open_price"] == "100"
    assert document["interval"] == "1h"
    assert document["open_time"] == "2026-09-25T15:00:00"


def test_candle_to_mongo_kline_reuses_mysql_boundary_conversion():
    document = candle_to_mongo_kline(candle())

    assert document.open_price == "100"
    assert document.close_price == "105"
    assert document.number_of_trades == 0
