import builtins
import importlib.util
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import prometheus_client
import pytest
from bson import ObjectId

import data_manager.api.routes.generic as generic_route
from data_manager.api.routes.generic import _decode_cursor, _encode_cursor
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.repositories.trade_repository import TradeRepository


def test_cursor_round_trip_and_sort_binding():
    value = datetime(2026, 1, 1, tzinfo=UTC)
    token = _encode_cursor([value, "trade-1"], [("timestamp", 1), ("trade_id", 1)])
    decoded = _decode_cursor(token, [("timestamp", 1)])
    assert decoded["sort"] == [("timestamp", 1), ("trade_id", 1)]
    assert decoded["values"] == [value, "trade-1"]


def test_cursor_round_trip_preserves_object_id():
    object_id = ObjectId("507f1f77bcf86cd799439011")
    token = _encode_cursor(
        ["2026-01-01T00:00:00+00:00", object_id],
        [("timestamp", 1), ("_id", 1)],
    )

    decoded = _decode_cursor(token, [("timestamp", 1)])

    assert decoded["values"] == ["2026-01-01T00:00:00+00:00", object_id]


def test_cursor_module_loads_without_bson(monkeypatch):
    original_import = builtins.__import__

    def import_without_bson(name, *args, **kwargs):
        if name == "bson":
            raise ImportError("bson unavailable")
        return original_import(name, *args, **kwargs)

    class CounterStub:
        def __init__(self, *args, **kwargs):
            pass

    module_name = "data_manager.api.routes.generic_without_bson"
    spec = importlib.util.spec_from_file_location(module_name, generic_route.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(builtins, "__import__", import_without_bson)
    monkeypatch.setattr(prometheus_client, "Counter", CounterStub)
    spec.loader.exec_module(module)

    assert module.ObjectId is None


def test_cursor_rejects_a_different_sort():
    token = _encode_cursor(["BTCUSDT", "trade-1"], [("symbol", 1), ("trade_id", 1)])
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


def test_legacy_cursor_is_rejected_after_encoding():
    token = _encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), "timestamp", 1)
    with pytest.raises(Exception, match="tiebreaker") as exc_info:
        _decode_cursor(token, [("timestamp", 1)])
    assert "tiebreaker" in str(exc_info.value)


def test_cursor_rejects_values_with_wrong_length():
    token = _encode_cursor(
        [datetime(2026, 1, 1, tzinfo=UTC)],
        [("timestamp", 1), ("trade_id", 1)],
    )
    with pytest.raises(Exception, match="values do not match") as exc_info:
        _decode_cursor(token, [("timestamp", 1)])
    assert "values do not match" in str(exc_info.value)


def test_cursor_rejects_object_id_when_support_is_unavailable(monkeypatch):
    token = _encode_cursor(
        [datetime(2026, 1, 1, tzinfo=UTC), ObjectId("507f1f77bcf86cd799439011")],
        [("timestamp", 1), ("_id", 1)],
    )
    monkeypatch.setattr(generic_route, "ObjectId", None)

    with pytest.raises(Exception, match="ObjectId support") as exc_info:
        _decode_cursor(token, [("timestamp", 1)])
    assert "ObjectId support" in str(exc_info.value)


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
