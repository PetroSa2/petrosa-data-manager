"""petrosa-data-manager#923: a Mongo error before the TTL check must reach the TTL fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import NetworkTimeout

from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.persistence_registry import ttl_policy_for_collection


@pytest.fixture
def adapter():
    a = MongoDBAdapter("mongodb://localhost:27017/test_db")
    a.client = MagicMock()
    a.db = MagicMock()
    a._connected = True
    return a


def _failing_collection() -> MagicMock:
    coll = MagicMock()
    coll.index_information = AsyncMock(side_effect=NetworkTimeout("timed out"))
    coll.create_indexes = AsyncMock()
    coll.create_index = AsyncMock()
    coll.drop_index = AsyncMock()
    return coll


@pytest.mark.asyncio
async def test_an_index_read_timeout_falls_back_to_the_ttl_index(adapter):
    collection = "cio_decisions"
    policy = ttl_policy_for_collection(collection)
    assert policy is not None  # the case that raised UnboundLocalError in production
    coll = _failing_collection()
    adapter.db.__getitem__ = MagicMock(return_value=coll)

    await adapter.ensure_indexes(collection)  # must not raise

    coll.create_index.assert_awaited_once()
    keys, options = (
        coll.create_index.await_args.args[0],
        coll.create_index.await_args.kwargs,
    )
    assert keys == [(policy.field, 1)]
    assert options["expireAfterSeconds"] == policy.seconds


@pytest.mark.asyncio
async def test_an_index_read_timeout_without_a_ttl_policy_does_not_raise(adapter):
    collection = "backfill_jobs"
    assert ttl_policy_for_collection(collection) is None
    coll = _failing_collection()
    adapter.db.__getitem__ = MagicMock(return_value=coll)

    await adapter.ensure_indexes(collection)

    coll.create_index.assert_not_awaited()
