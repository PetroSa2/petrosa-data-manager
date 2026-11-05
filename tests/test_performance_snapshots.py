"""Strategy-performance snapshots served off the request path (petrosa-data-manager#738)."""

from __future__ import annotations

import asyncio

import pytest

from data_manager.services import performance_snapshots as ps


def _good(win_rate: float = 0.5) -> dict:
    return {
        "stats": {"win_rate": win_rate, "wins": 1, "losses": 1},
        "metadata": {"strategy_id": "s", "history_status": "computed"},
    }


def _degraded() -> dict:
    return {
        "degraded": True,
        "stats": {"win_rate": None},
        "metadata": {"history_status": "unavailable", "degraded": True},
    }


def test_interval_uses_env_and_labelled_fallback(monkeypatch):
    monkeypatch.delenv(ps.INTERVAL_ENV, raising=False)
    assert ps.interval_seconds() == ps.INTERVAL_FALLBACK_SECONDS
    monkeypatch.setenv(ps.INTERVAL_ENV, "30")
    assert ps.interval_seconds() == 30.0
    for bad in ("0", "-5", "abc"):
        monkeypatch.setenv(ps.INTERVAL_ENV, bad)
        assert ps.interval_seconds() == ps.INTERVAL_FALLBACK_SECONDS


def test_get_returns_copy_with_snapshot_metadata():
    store = ps.PerformanceSnapshotStore(interval=60)
    assert store.get("s") is None
    assert store.put("s", _good())
    served = store.get("s")
    assert served["stats"]["win_rate"] == 0.5
    assert served["metadata"]["snapshot_status"] == "served"
    assert served["metadata"]["snapshot_age_seconds"] >= 0
    assert "snapshot_computed_at" in served["metadata"]
    served["stats"]["win_rate"] = 0.9
    assert store.get("s")["stats"]["win_rate"] == 0.5


def test_degraded_body_never_replaces_good_snapshot():
    store = ps.PerformanceSnapshotStore(interval=60)
    assert not store.put("s", _degraded())
    assert store.get("s") is None
    store.put("s", _good(0.4))
    assert not store.put("s", _degraded())
    assert store.get("s")["stats"]["win_rate"] == 0.4


def test_zero_sample_body_is_good():
    body = {
        "stats": {"win_rate": 0.0, "wins": 0, "losses": 0},
        "metadata": {"history_status": "empty", "fills_replayed": 0},
    }
    assert ps.is_good(body)


def test_stale_snapshot_is_not_served(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ps, "monotonic", lambda: clock[0])
    store = ps.PerformanceSnapshotStore(interval=10)
    store.put("s", _good())
    clock[0] += 10 * ps.STALE_AFTER_INTERVALS
    assert store.get("s") is not None
    clock[0] += 0.001
    assert store.get("s") is None


def test_idle_strategies_are_forgotten(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ps, "monotonic", lambda: clock[0])
    store = ps.PerformanceSnapshotStore(interval=10)
    store.note_request("old")
    store.put("old", _good())
    clock[0] += 10 * ps.IDLE_AFTER_INTERVALS + 1
    store.note_request("new")
    assert store.wanted() == ["new"]
    assert store.get("old") is None


def test_refresh_sets_context_and_stores_each_strategy():
    store = ps.PerformanceSnapshotStore(interval=5)
    store.note_request("a")
    store.note_request("b")
    seen: dict[str, bool] = {}

    async def compute(strategy_id: str) -> dict:
        seen[strategy_id] = ps.snapshot_refresh_active.get()
        if strategy_id == "b":
            return _degraded()
        return _good()

    outcomes = asyncio.run(store.refresh_once(compute))
    assert outcomes == {"a": "stored", "b": "kept_previous"}
    assert seen == {"a": True, "b": True}
    assert ps.snapshot_refresh_active.get() is False
    assert store.get("a") is not None
    assert store.get("b") is None


def test_refresh_survives_errors_and_timeouts():
    store = ps.PerformanceSnapshotStore(interval=0.05)
    for name in ("boom", "slow", "ok"):
        store.note_request(name)

    async def compute(strategy_id: str) -> dict:
        if strategy_id == "boom":
            raise RuntimeError("db down")
        if strategy_id == "slow":
            await asyncio.sleep(1)
        return _good()

    outcomes = asyncio.run(store.refresh_once(compute))
    assert outcomes == {"boom": "error", "ok": "stored", "slow": "error"}


def test_run_loops_until_cancelled():
    store = ps.PerformanceSnapshotStore(interval=0.01)
    store.note_request("s")
    calls: list[str] = []

    async def compute(strategy_id: str) -> dict:
        calls.append(strategy_id)
        return _good()

    async def scenario() -> None:
        task = asyncio.create_task(store.run(compute))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(calls) >= 2
