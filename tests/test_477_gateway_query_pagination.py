from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.api.routes.generic import _decode_cursor, _encode_cursor
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.repositories.trade_repository import TradeRepository


def test_cursor_round_trip_and_sort_binding():
    value = datetime(2026, 1, 1, tzinfo=UTC)
    token = _encode_cursor([value, "trade-1"], [("timestamp", 1), ("trade_id", 1)])
    decoded = _decode_cursor(token, [("timestamp", 1)])
    assert decoded["sort"] == [("timestamp", 1), ("trade_id", 1)]
    assert decoded["values"] == [value, "trade-1"]


def test_cursor_rejects_a_different_sort():
    token = _encode_cursor(
        ["BTCUSDT", "trade-1"], [("symbol", 1), ("trade_id", 1)]
    )
    with pytest.raises(Exception, match="does not match") as exc_info:
        _decode_cursor(token, [("timestamp", 1)])
    assert "does not match" in str(exc_info.value)


def test_cursor_rejects_a_non_scalar_value():
    token = _encode_cursor(
        [{"unexpected": "object"}, "trade-1"], [("timestamp", 1), ("trade_id", 1)]
    )
    with pytest.raises(Exception, match="scalar") as exc_info:
        _decode_cursor(token, [("timestamp", 1)])
        assert "scalar" in str(exc_info.value)


def test_cursor_rejects_missing_unique_tiebreaker():
    token = _encode_cursor(["2026-01-01T00:00:00+00:00"], [("timestamp", 1)])
    with pytest.raises(Exception, match="tiebreaker"):
        _decode_cursor(token, [("timestamp", 1)])


@pytest.mark.asyncio
async def test_mongodb_cursor_page_returns_an_exclusive_next_cursor():
    adapter = MongoDBAdapter("mongodb://localhost:27017/test_db")
    adapter._connected = True
    adapter.db = MagicMock()
    db_cursor = MagicMock()
    db_cursor.sort.return_value = db_cursor
    db_cursor.skip.return_value = db_cursor
    db_cursor.limit.return_value = db_cursor
    db_cursor.to_list = AsyncMock(
        return_value=[
            {"timestamp": datetime(2026, 1, 1, tzinfo=UTC)},
            {"timestamp": datetime(2026, 1, 2, tzinfo=UTC)},
        ]
    )
    collection = MagicMock()
    collection.find.return_value = db_cursor
    collection.count_documents = AsyncMock(return_value=2)
    adapter.db.__getitem__ = MagicMock(return_value=collection)

    records, total, next_cursor = await adapter.find_paginated(
        "trades_BTCUSDT",
        sort_list=[("timestamp", 1)],
        limit=1,
        include_cursor=True,
    )

    assert len(records) == 1
    assert total == 2
    assert next_cursor["values"] == [datetime(2026, 1, 1, tzinfo=UTC), None]
    db_cursor.limit.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_mongodb_cursor_page_at_end_has_no_next_cursor():
    adapter = MongoDBAdapter("mongodb://localhost:27017/test_db")
    adapter._connected = True
    adapter.db = MagicMock()
    db_cursor = MagicMock()
    db_cursor.sort.return_value = db_cursor
    db_cursor.skip.return_value = db_cursor
    db_cursor.limit.return_value = db_cursor
    db_cursor.to_list = AsyncMock(return_value=[])
    collection = MagicMock()
    collection.find.return_value = db_cursor
    collection.count_documents = AsyncMock(return_value=0)
    adapter.db.__getitem__ = MagicMock(return_value=collection)

    records, total, next_cursor = await adapter.find_paginated(
        "trades_BTCUSDT",
        sort_list=[("timestamp", 1)],
        limit=1,
        include_cursor=True,
    )

    assert records == []
    assert total == 0
    assert next_cursor is None


@pytest.mark.asyncio
async def test_trade_range_forwards_bounded_query_to_mongodb():
    mongodb = MagicMock()
    mongodb.query_range = AsyncMock(return_value=[{"trade_id": 1}])
    repo = TradeRepository(mysql_adapter=None, mongodb_adapter=mongodb)

    result = await repo.get_range(
        "BTCUSDT",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        limit=101,
        offset=20,
        descending=True,
    )

    assert result == [{"trade_id": 1}]
    mongodb.query_range.assert_awaited_once_with(
        "trades_BTCUSDT",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        "BTCUSDT",
        limit=101,
        offset=20,
        descending=True,
    )
