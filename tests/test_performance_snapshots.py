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
    assert served["degraded"] is False
    assert served["metadata"]["degraded"] is False
    assert served["metadata"]["snapshot_age_seconds"] >= 0
    assert "snapshot_computed_at" in served["metadata"]
    served["stats"]["win_rate"] = 0.9
    assert store.get("s")["stats"]["win_rate"] == 0.5


def test_warm_start_restores_age_and_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ps, "monotonic", lambda: clock[0])
    computed_at = ps.datetime.now(ps.UTC)

    async def load():
        return [{"strategy_id": "s", "body": _good(), "computed_at": computed_at}]

    store = ps.PerformanceSnapshotStore(interval=10, load=load)
    asyncio.run(store.warm_start())
    assert store.get("s")["metadata"]["snapshot_status"] == "served"
    clock[0] += 10 * ps.STALE_AFTER_INTERVALS + 1
    assert store.get("s")["metadata"]["stale"] is True
    clock[0] += 10 * (ps.EXPIRE_AFTER_INTERVALS - ps.STALE_AFTER_INTERVALS)
    assert store.get("s")["metadata"]["snapshot_status"] == "expired"


def test_refresh_persists_only_good_snapshots():
    persisted: list[tuple[str, dict, object]] = []

    async def persist(strategy_id, body, computed_at):
        persisted.append((strategy_id, body, computed_at))

    store = ps.PerformanceSnapshotStore(interval=60, persist=persist)
    store.note_request("good")
    store.note_request("bad")

    async def compute(strategy_id: str) -> dict:
        return _good() if strategy_id == "good" else _degraded()

    assert asyncio.run(store.refresh_once(compute)) == {
        "bad": "kept_previous",
        "good": "stored",
    }
    assert [item[0] for item in persisted] == ["good"]
    assert persisted[0][1] == _good()


def test_warm_start_skips_invalid_records():
    async def load():
        return [
            {
                "strategy_id": "missing-body",
                "body": None,
                "computed_at": ps.datetime.now(ps.UTC),
            },
            {"strategy_id": "bad-date", "body": _good(), "computed_at": "not-a-date"},
            {"strategy_id": "missing-date", "body": _good()},
        ]

    store = ps.PerformanceSnapshotStore(interval=60, load=load)
    asyncio.run(store.warm_start())
    assert store.get("missing-body") is None
    assert store.get("bad-date") is None
    assert store.get("missing-date") is None


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
    assert stale["degraded"] is False and stale["metadata"]["degraded"] is False
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
    assert "pass seed=none strategies=1" in caplog.text
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


def test_seed_runs_inside_the_refresh_context():
    store = ps.PerformanceSnapshotStore(interval=60)
    seen: list[bool] = []

    async def seed() -> list[str]:
        seen.append(ps.snapshot_refresh_active.get())
        return ["a"]

    async def compute(strategy_id: str) -> dict:
        return _good()

    asyncio.run(store.refresh_once(compute, seed))
    assert seen == [True]
    assert ps.snapshot_refresh_active.get() is False


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
            # Bounded: a swallowed cancel fails the test instead of hanging the run.
            await asyncio.wait_for(asyncio.shield(task), 2)

    asyncio.run(scenario())
    assert len(calls) >= 2


def test_cancel_is_never_swallowed_by_a_finishing_compute():
    """A cancel landing while a compute finishes stops the loop (it did not, ~50 % of runs, with wait_for)."""

    async def compute(_strategy_id: str) -> dict:
        return _good()

    async def scenario() -> bool:
        store = ps.PerformanceSnapshotStore(interval=0.0001)
        store.note_request("s")
        task = asyncio.create_task(store.run(compute))
        await asyncio.sleep(0.002)
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        except asyncio.CancelledError:
            return True
        except TimeoutError:
            task.cancel()
            return False
        return False

    results = [asyncio.run(scenario()) for _ in range(200)]
    assert all(results), f"{results.count(False)} of 200 cancels were swallowed"


def _stops_within_bound(compute, runs: int) -> list[bool]:
    async def scenario() -> bool:
        store = ps.PerformanceSnapshotStore(interval=0.0001)
        store.note_request("s")
        task = asyncio.create_task(store.run(compute))
        await asyncio.sleep(0.002)
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        except asyncio.CancelledError:
            return True
        except TimeoutError:
            task.cancel()
            return False
        return False

    return [asyncio.run(scenario()) for _ in range(runs)]


def test_cancel_swallowed_inside_compute_still_stops_the_loop():
    """A compute that swallows the CancelledError and returns must not keep the loop alive."""

    swallowed = [0]

    async def swallowing_compute(_strategy_id: str) -> dict:
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            # What an inner wait_for does on Python 3.11 in the race. Only the first cancel is
            # swallowed, so a broken loop still ends at the harness's second cancel (bounded test).
            if swallowed[0]:
                raise
            swallowed[0] += 1
        return _good()

    results = []
    for _ in range(50):
        swallowed[0] = 0
        results.extend(_stops_within_bound(swallowing_compute, 1))
    assert all(results), f"{results.count(False)} of 50 runs kept looping"


def test_cancel_racing_an_inner_wait_for_stops_the_loop():
    async def compute_with_inner_wait_for(_strategy_id: str) -> dict:
        for _ in range(5):
            await asyncio.wait_for(asyncio.sleep(0), 1)
        return _good()

    results = _stops_within_bound(compute_with_inner_wait_for, 200)
    assert all(results), f"{results.count(False)} of 200 cancels were swallowed"


def test_snapshot_past_expiry_returns_expired_metadata_only(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ps, "monotonic", lambda: clock[0])
    store = ps.PerformanceSnapshotStore(interval=10)
    store.put("s", _good())
    clock[0] += 10 * ps.EXPIRE_AFTER_INTERVALS
    assert store.get("s")["metadata"]["snapshot_status"] == "stale"
    clock[0] += 0.001
    expired = store.get("s")
    assert expired == {
        "metadata": {
            "snapshot_status": "expired",
            "snapshot_age_seconds": pytest.approx(100.001),
            "snapshot_computed_at": expired["metadata"]["snapshot_computed_at"],
        }
    }
    assert "stats" not in expired


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict]] = []

    def add(self, value, attributes):
        self.calls.append((value, attributes))

    def set(self, value, attributes):
        self.calls.append((value, attributes))


def test_pass_records_outcome_counter_age_gauge_and_seed_status(monkeypatch, caplog):
    counter, gauge = _Recorder(), _Recorder()
    monkeypatch.setattr(ps, "PERFORMANCE_SNAPSHOT_REFRESH", counter)
    monkeypatch.setattr(ps, "PERFORMANCE_SNAPSHOT_AGE", gauge)
    monkeypatch.setattr(ps.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.025)
    store = ps.PerformanceSnapshotStore(interval=60)
    store.note_request("bad")

    async def compute(strategy_id: str) -> dict:
        if strategy_id == "bad":
            raise RuntimeError("db down")
        return _good()

    async def seed() -> list[str]:
        return ["good"]

    async def broken_seed() -> list[str]:
        raise RuntimeError("registry down")

    with caplog.at_level("INFO", logger=ps.__name__):
        asyncio.run(store.refresh_once(compute, seed))
        asyncio.run(store.refresh_once(compute, broken_seed))
        asyncio.run(store.refresh_once(compute))

    assert counter.calls[:2] == [
        (1, {"strategy_id": "bad", "outcome": "error"}),
        (1, {"strategy_id": "good", "outcome": "stored"}),
    ]
    assert [attributes for _, attributes in gauge.calls] == [
        {"strategy_id": "good"}
    ] * 3
    assert all(age >= 0 for age, _ in gauge.calls)
    assert "pass seed=ok strategies=2" in caplog.text
    assert "pass seed=error strategies=2" in caplog.text
    assert "pass seed=none strategies=2" in caplog.text


def test_compute_raising_cancelled_error_is_an_error_not_a_stop():
    """A CancelledError the compute raises itself (a poisoned shared task) must not stop the loop (#738)."""
    store = ps.PerformanceSnapshotStore(interval=60)
    store.note_request("poisoned")
    store.note_request("ok")

    async def compute(strategy_id: str) -> dict:
        if strategy_id == "poisoned":
            raise asyncio.CancelledError
        return _good()

    async def seed() -> list[str]:
        raise asyncio.CancelledError

    outcomes = asyncio.run(store.refresh_once(compute, seed))
    assert outcomes == {"ok": "stored", "poisoned": "error"}


class _GaugeRecorder:
    def __init__(self) -> None:
        self.values: list[int] = []

    def set(self, value, attributes):
        assert attributes == {"component": "performance_snapshots"}
        self.values.append(value)


def test_supervisor_restarts_a_loop_that_exits(monkeypatch, caplog):
    gauge = _GaugeRecorder()
    monkeypatch.setattr(ps, "STARTUP_COMPONENT_UP", gauge)
    store = ps.PerformanceSnapshotStore(interval=0.01)
    runs: list[int] = []

    async def fake_run(compute, seed=None, warm_only=None):
        runs.append(1)
        if len(runs) == 1:
            raise asyncio.CancelledError  # a stray cancel, not a shutdown
        if len(runs) == 2:
            raise RuntimeError("unexpected")
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "run", fake_run)

    async def scenario() -> None:
        task = asyncio.create_task(ps.supervise(store, lambda _s: None))
        await asyncio.sleep(0.2)
        assert len(runs) == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)

    with caplog.at_level("ERROR", logger=ps.__name__):
        asyncio.run(scenario())
    assert "refresh loop exited unexpectedly (CancelledError)" in caplog.text
    assert "refresh loop exited unexpectedly (RuntimeError)" in caplog.text
    assert gauge.values == [1, 0, 1, 0, 1, 0]


# --- dm#878: the netting snapshots are only a warm fallback while ground truth is served ------


class _LoopClock:
    """Drives ``store.run``: a fake monotonic clock that sleep() advances; stops after ``ticks``."""

    def __init__(self, monkeypatch, ticks: int):
        self.now = 1000.0
        self.ticks = ticks
        monkeypatch.setattr(ps, "monotonic", lambda: self.now)

        async def fake_sleep(seconds):
            self.now += seconds
            self.ticks -= 1
            if self.ticks <= 0:
                raise asyncio.CancelledError

        monkeypatch.setattr(ps.asyncio, "sleep", fake_sleep)


def _run(store, compute, warm_only):
    with pytest.raises(asyncio.CancelledError) as stopped:
        asyncio.run(store.run(compute, lambda: _seed(), warm_only))
    assert stopped.type is asyncio.CancelledError


async def _seed():
    return ["s1"]


def test_ground_truth_paces_the_netting_refresh_to_the_warm_fallback(monkeypatch):
    clock = _LoopClock(monkeypatch, ticks=31)  # 31 one-minute ticks
    passes = []

    async def compute(strategy_id):
        passes.append(clock.now)
        return _good()

    async def warm_only():
        return True

    store = ps.PerformanceSnapshotStore(interval=60)
    _run(store, compute, warm_only)
    # one pass at start, then one every 15 minutes, not 31 back-to-back passes
    assert len(passes) == 3
    assert passes[1] - passes[0] == pytest.approx(ps.WARM_FALLBACK_SECONDS)
    assert store.slow_pace is True and store.cadence == ps.WARM_FALLBACK_SECONDS


def test_netting_keeps_the_normal_cadence(monkeypatch):
    clock = _LoopClock(monkeypatch, ticks=5)
    passes = []

    async def compute(strategy_id):
        passes.append(clock.now)
        return _good()

    async def warm_only():
        return False

    store = ps.PerformanceSnapshotStore(interval=60)
    _run(store, compute, warm_only)
    assert len(passes) == 5 and store.slow_pace is False


def test_a_flip_back_to_netting_refreshes_within_one_interval_and_never_serves_expired(
    monkeypatch,
):
    clock = _LoopClock(monkeypatch, ticks=14)
    mode = {"ground_truth": True}
    passes = []
    served_before_fresh_pass = []
    store = ps.PerformanceSnapshotStore(interval=60)

    async def compute(strategy_id):
        if not mode["ground_truth"]:
            # the first normal pass: what a reader sees right now must not be "expired"
            served_before_fresh_pass.append(store.get(strategy_id))
        passes.append(clock.now)
        return _good()

    async def warm_only():
        if clock.now - 1000.0 >= 12 * 60:  # flip back to netting after 12 minutes
            mode["ground_truth"] = False
        return mode["ground_truth"]

    _run(store, compute, warm_only)
    # one slow pass at t=0, then the flip at 12 min refreshes at once (no 15-minute wait)
    assert passes[0] == 1000.0 and passes[1] - 1000.0 == pytest.approx(12 * 60)
    seen = served_before_fresh_pass[0]
    assert seen["metadata"]["snapshot_status"] != "expired"
    assert seen["stats"]["win_rate"] == 0.5
    assert (
        store.slow_pace is False
    )  # a normal pass completed: back to the normal horizons


def test_an_unreadable_source_keeps_the_normal_cadence(monkeypatch):
    clock = _LoopClock(monkeypatch, ticks=3)
    passes = []

    async def compute(strategy_id):
        passes.append(clock.now)
        return _good()

    async def warm_only():
        raise RuntimeError("config read failed")

    _run(ps.PerformanceSnapshotStore(interval=60), compute, warm_only)
    assert len(passes) == 3
