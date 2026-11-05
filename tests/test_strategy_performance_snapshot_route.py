"""The performance route serves background snapshots (petrosa-data-manager#738)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

import constants
import data_manager.api.routes.analysis as analysis
from data_manager.services import (
    historic_analytics,
    performance_snapshots as ps,
)


@pytest.fixture
def store(monkeypatch):
    fresh = ps.PerformanceSnapshotStore(interval=60)
    monkeypatch.setattr(ps, "store", fresh)
    return fresh


def _good(win_rate: float) -> dict:
    return {
        "stats": {"win_rate": win_rate, "wins": 3, "losses": 1},
        "metadata": {"strategy_id": "S1", "history_status": "computed"},
    }


@pytest.mark.asyncio
async def test_route_serves_snapshot_without_computing(store, monkeypatch):
    compute = AsyncMock(side_effect=AssertionError("must not compute inline"))
    monkeypatch.setattr(analysis, "compute_strategy_performance", compute)
    store.put("S1", _good(0.75))

    started = time.monotonic()
    response = await analysis.get_strategy_performance("S1")

    assert time.monotonic() - started < 0.1
    assert response["stats"]["win_rate"] == 0.75
    assert response["metadata"]["snapshot_status"] == "served"
    assert "degraded" not in response
    compute.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_miss_returns_warming_without_db_or_compute(store, monkeypatch):
    compute = AsyncMock(side_effect=AssertionError("must not compute inline"))
    monkeypatch.setattr(analysis, "compute_strategy_performance", compute)

    started = time.monotonic()
    response = await analysis.get_strategy_performance("S1")

    assert time.monotonic() - started < 0.1
    compute.assert_not_awaited()
    assert response["degraded"] is True
    assert response["error"]["type"] == "PerformanceSnapshotWarming"
    assert response["metadata"]["snapshot_status"] == "warming"
    assert response["metadata"]["snapshot_refresh_seconds"] == store.cadence
    assert set(response["stats"]) >= {
        "win_rate",
        "win_rate_delta",
        "consecutive_losses",
    }
    assert store.wanted() == ["S1"]
    # Only the background refresh stores snapshots; the request path never writes one.
    assert store.get("S1") is None


@pytest.mark.asyncio
async def test_route_serves_stale_snapshot_flagged(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ps, "monotonic", lambda: clock[0])
    store.put("S1", _good(0.4))
    clock[0] += store.cadence * ps.STALE_AFTER_INTERVALS + 1

    response = await analysis.get_strategy_performance("S1")

    assert response["stats"]["win_rate"] == 0.4
    assert response["metadata"]["snapshot_status"] == "stale"
    assert response["metadata"]["stale"] is True


@pytest.mark.asyncio
async def test_refresh_pass_fills_snapshot_served_by_next_request(store, monkeypatch):
    compute = AsyncMock(return_value=_good(0.6))
    monkeypatch.setattr(analysis, "compute_strategy_performance", compute)
    await analysis.get_strategy_performance("S1")

    outcomes = await store.refresh_once(analysis.compute_strategy_performance)
    compute.reset_mock(side_effect=AssertionError("must not compute inline"))
    response = await analysis.get_strategy_performance("S1")

    assert outcomes == {"S1": "stored"}
    assert response["metadata"]["snapshot_status"] == "served"
    assert response["stats"]["win_rate"] == 0.6


def test_background_refresh_gives_only_the_read_the_full_budget():
    assert analysis._execution_events_timeout() == pytest.approx(
        analysis._PERFORMANCE_EXECUTION_EVENTS_TIMEOUT_SECONDS
    )
    token = ps.snapshot_refresh_active.set(True)
    try:
        full = constants.ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS
        assert analysis._execution_events_timeout() == pytest.approx(full)
        # The overlay keeps its request budget: measured live, a 5 s overlay still timed out and only
        # added 5 s to every refresh.
        assert not hasattr(analysis, "_overlay_timeout")
    finally:
        ps.snapshot_refresh_active.reset(token)


@pytest.mark.asyncio
async def test_slow_mysql_read_times_out_on_request_but_succeeds_in_refresh(
    monkeypatch,
):
    """The measured failure: a read slower than the 1 s request deadline (about 2 round trips)."""
    delay = analysis._PERFORMANCE_EXECUTION_EVENTS_TIMEOUT_SECONDS + 0.2
    assert delay < constants.ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS
    rows = [{"strategy_id": "S1", "event_type": "filled", "timestamp": 1}]

    def slow_read(_mysql, **_kwargs):
        time.sleep(delay)
        return rows, "fill-1"

    monkeypatch.setattr(historic_analytics, "get_mysql_adapter", lambda _db: object())
    monkeypatch.setattr(historic_analytics, "read_execution_events", slow_read)
    monkeypatch.setattr(analysis, "_EXECUTION_EVENTS_CACHE", None)

    with pytest.raises(analysis._AnalyticsDependencyTimeout):
        await analysis._load_execution_events_once(None, "S1")

    monkeypatch.setattr(analysis, "_EXECUTION_EVENTS_CACHE", None)
    token = ps.snapshot_refresh_active.set(True)
    try:
        loaded, latest = await analysis._load_execution_events_once(None, "S1")
    finally:
        ps.snapshot_refresh_active.reset(token)
    assert loaded == rows
    assert latest == "fill-1"
