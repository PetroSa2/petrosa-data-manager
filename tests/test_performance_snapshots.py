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


def test_stale_snapshot_is_served_flagged_with_age(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ps, "monotonic", lambda: clock[0])
    store = ps.PerformanceSnapshotStore(interval=10)
    store.put("s", _good())
    clock[0] += 10 * ps.STALE_AFTER_INTERVALS
    assert store.get("s")["metadata"]["snapshot_status"] == "served"
    clock[0] += 0.001
    stale = store.get("s")
    assert stale["metadata"]["snapshot_status"] == "stale"
    assert stale["metadata"]["stale"] is True
    assert stale["metadata"]["snapshot_age_seconds"] == pytest.approx(30.001)
    assert stale["stats"]["win_rate"] == 0.5


def test_cadence_follows_a_pass_longer_than_the_interval():
    store = ps.PerformanceSnapshotStore(interval=10)
    assert store.cadence == 10
    store.last_pass_seconds = 25
    assert store.cadence == 25


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


def test_refresh_survives_errors_and_timeouts(monkeypatch):
    monkeypatch.setattr(ps.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.025)
    store = ps.PerformanceSnapshotStore(interval=60)
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


def test_compute_over_budget_keeps_previous_snapshot(monkeypatch, caplog):
    monkeypatch.setattr(ps.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.025)
    assert ps.compute_cap_seconds() == pytest.approx(0.05)
    store = ps.PerformanceSnapshotStore(interval=60)
    store.note_request("s")
    store.put("s", _good(0.3))

    async def slow(_strategy_id: str) -> dict:
        await asyncio.sleep(1)
        return _good(0.9)

    with caplog.at_level("INFO", logger=ps.__name__):
        outcomes = asyncio.run(store.refresh_once(slow))
    assert outcomes == {"s": "error"}
    assert store.get("s")["stats"]["win_rate"] == 0.3
    assert "refresh outcome=error strategy=s" in caplog.text
    assert "pass strategies=1" in caplog.text
    assert "error=1" in caplog.text


def test_seed_registers_registry_strategies_and_survives_failure():
    store = ps.PerformanceSnapshotStore(interval=60)
    computed: list[str] = []

    async def compute(strategy_id: str) -> dict:
        computed.append(strategy_id)
        return _good()

    async def seed() -> list[str]:
        return ["b", "a", ""]

    async def broken_seed() -> list[str]:
        raise RuntimeError("registry down")

    assert asyncio.run(store.refresh_once(compute, seed)) == {
        "a": "stored",
        "b": "stored",
    }
    assert computed == ["a", "b"]
    assert asyncio.run(store.refresh_once(compute, broken_seed)) == {
        "a": "stored",
        "b": "stored",
    }


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
