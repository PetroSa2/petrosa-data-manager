"""The analysis routes honour the netting | ground_truth switch (petrosa-data-manager#842)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis
from data_manager.services import (
    performance_snapshots as ps,
    strategy_stats_source as src,
)

pytestmark = pytest.mark.asyncio


def _snapshot():
    return {
        "computed_at": datetime.now(UTC),
        "rule": "A",
        "window": {},
        "reconciliation": {"gap": 0.0},
        "strategies": {
            "iceberg_detector": {
                "performance": {
                    "win_rate": 0.4,
                    "wins": 10,
                    "losses": 15,
                    "consecutive_losses": 1,
                },
                "rounds": {"closed_rounds": 25, "wins": 10, "losses": 15, "fills": 900},
            },
            "spread_liquidity": {
                "performance": {
                    "win_rate": 0.5,
                    "wins": 6,
                    "losses": 6,
                    "consecutive_losses": 3,
                },
                "rounds": {"closed_rounds": 12, "wins": 6, "losses": 6, "fills": 200},
            },
        },
    }


@pytest.fixture
def switched(monkeypatch):
    src.reset_cache()
    store = ps.PerformanceSnapshotStore(interval=60)
    monkeypatch.setattr(ps, "store", store)
    monkeypatch.setattr(analysis, "_ground_truth_snapshot_cache", None)

    def set_source(value):
        async def get_source(_db, clock=None):
            return value

        monkeypatch.setattr(src, "get_source", get_source)

    yield set_source, store
    src.reset_cache()


async def test_ground_truth_source_serves_the_exchange_truth_performance(
    switched, monkeypatch
):
    set_source, store = switched
    set_source("ground_truth")
    monkeypatch.setattr(
        analysis, "_ground_truth_snapshot", AsyncMock(return_value=_snapshot())
    )
    store.put("iceberg_detector", {"stats": {"wins": 3, "losses": 3}, "metadata": {}})

    body = await analysis.get_strategy_performance("iceberg_detector")

    assert (body["stats"]["wins"], body["stats"]["losses"]) == (
        10,
        15,
    )  # not the netting 3/3
    assert body["metadata"]["stats_source"] == "ground_truth"


async def test_netting_source_keeps_the_existing_path(switched, monkeypatch):
    set_source, store = switched
    set_source("netting")
    gt = AsyncMock(side_effect=AssertionError("ground truth must not be read"))
    monkeypatch.setattr(analysis, "_ground_truth_snapshot", gt)
    store.put("iceberg_detector", {"stats": {"wins": 3, "losses": 3}, "metadata": {}})

    body = await analysis.get_strategy_performance("iceberg_detector")

    assert (body["stats"]["wins"], body["stats"]["losses"]) == (3, 3)
    gt.assert_not_awaited()


async def test_ground_truth_rounds_all_and_one_strategy(switched, monkeypatch):
    set_source, _ = switched
    set_source("ground_truth")
    monkeypatch.setattr(
        analysis, "_ground_truth_snapshot", AsyncMock(return_value=_snapshot())
    )

    every = await analysis.get_closed_rounds(strategy_id=None, window_days=30.0)
    one = await analysis.get_closed_rounds(
        strategy_id="spread_liquidity", window_days=30.0
    )

    assert set(every["strategies"]) == {"iceberg_detector", "spread_liquidity"}
    assert every["metadata"]["stats_source"] == "ground_truth"
    assert set(one["strategies"]) == {"spread_liquidity"}


async def test_snapshot_read_is_cached_and_a_slow_read_keeps_the_last_one(monkeypatch):
    first = _snapshot()
    reads = []

    class _Store:
        def __init__(self, _adapter):
            pass

        async def latest(self):
            reads.append(1)
            if len(reads) == 1:
                return first
            await asyncio.sleep(10)  # a hung MongoDB

    monkeypatch.setattr(analysis, "StrategyStatsSnapshotStore", _Store)
    monkeypatch.setattr(analysis, "_GROUND_TRUTH_SNAPSHOT_READ_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(analysis, "_ground_truth_snapshot_cache", None)
    monkeypatch.setattr(api_module, "db_manager", MagicMock())
    clock = [100.0]
    monkeypatch.setattr(analysis, "monotonic", lambda: clock[0])

    assert await analysis._ground_truth_snapshot() is first
    clock[0] = 130.0
    assert await analysis._ground_truth_snapshot() is first  # cached, no read
    assert len(reads) == 1
    clock[0] = 200.0
    assert (
        await analysis._ground_truth_snapshot() is first
    )  # read timed out: the last one is kept
    assert len(reads) == 2
