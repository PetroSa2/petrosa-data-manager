"""The exchange-truth stats snapshot store (petrosa-data-manager#842)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.services import strategy_stats_snapshot_store as store_module
from data_manager.services.strategy_stats_snapshot_store import (
    COLLECTION,
    StrategyStatsSnapshotStore,
    is_stale,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)


def _adapter(latest=None):
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    collection.find_one = AsyncMock(return_value=latest)
    adapter = MagicMock()
    adapter.db = {COLLECTION: collection}
    adapter.ensure_indexes = AsyncMock()
    return adapter, collection


async def test_save_ensures_the_ttl_index_once_then_inserts():
    adapter, collection = _adapter()
    store = StrategyStatsSnapshotStore(adapter)
    await store.save({"computed_at": NOW, "strategies": {}})
    await store.save({"computed_at": NOW, "strategies": {}})
    adapter.ensure_indexes.assert_awaited_once_with(COLLECTION)
    assert collection.insert_one.await_count == 2


async def test_save_refuses_a_snapshot_without_a_datetime_computed_at():
    adapter, collection = _adapter()
    with pytest.raises(ValueError) as err:
        await StrategyStatsSnapshotStore(adapter).save({"computed_at": "2026-11-06"})
    assert "computed_at" in str(err.value)
    collection.insert_one.assert_not_awaited()


async def test_latest_reads_the_newest_and_drops_the_mongo_id():
    adapter, collection = _adapter(latest={"_id": "x", "computed_at": NOW, "rule": "A"})
    latest = await StrategyStatsSnapshotStore(adapter).latest()
    assert latest == {"computed_at": NOW, "rule": "A"}
    assert collection.find_one.await_args.kwargs["sort"] == [("computed_at", -1)]
    adapter_empty, _ = _adapter(latest=None)
    assert await StrategyStatsSnapshotStore(adapter_empty).latest() is None


async def test_staleness_is_two_refresh_cycles(monkeypatch):
    monkeypatch.setattr(store_module, "REFRESH_INTERVAL_SECONDS", 900.0)
    fresh = {"computed_at": NOW - timedelta(minutes=29)}
    old = {"computed_at": NOW - timedelta(minutes=31)}
    assert is_stale(fresh, now=NOW) is False
    assert is_stale(old, now=NOW) is True
    assert (
        is_stale({"computed_at": (NOW - timedelta(minutes=5)).isoformat()}, now=NOW)
        is False
    )
    assert is_stale(None, now=NOW) is True
    assert is_stale({"computed_at": "garbage"}, now=NOW) is True
    assert is_stale({}, now=NOW) is True
