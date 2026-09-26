"""get_record_count must not send limit=None to count_documents (Mongo rejects {"$limit": null})."""

from unittest.mock import AsyncMock

import pytest

from data_manager.db.mongodb_adapter import MongoDBAdapter


def _adapter_with(coll):
    adapter = MongoDBAdapter.__new__(MongoDBAdapter)
    adapter._connected = True
    adapter.db = {"klines_1h": coll}
    return adapter


@pytest.mark.asyncio
async def test_no_max_count_sends_no_limit():
    coll = AsyncMock()
    coll.count_documents = AsyncMock(return_value=42)
    adapter = _adapter_with(coll)

    assert await adapter.get_record_count("klines_1h", symbol="BTCUSDT") == 42

    _, kwargs = coll.count_documents.call_args
    assert "limit" not in kwargs


@pytest.mark.asyncio
async def test_positive_max_count_is_forwarded_as_limit():
    coll = AsyncMock()
    coll.count_documents = AsyncMock(return_value=5)
    adapter = _adapter_with(coll)

    assert (
        await adapter.get_record_count("klines_1h", symbol="BTCUSDT", max_count=5) == 5
    )

    _, kwargs = coll.count_documents.call_args
    assert kwargs["limit"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("max_count", [0, -3])
async def test_non_positive_max_count_is_not_forwarded(max_count):
    coll = AsyncMock()
    coll.count_documents = AsyncMock(return_value=0)
    adapter = _adapter_with(coll)

    await adapter.get_record_count("klines_1h", max_count=max_count)

    _, kwargs = coll.count_documents.call_args
    assert "limit" not in kwargs
