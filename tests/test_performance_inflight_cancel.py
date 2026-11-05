"""A cancelled waiter never poisons the shared in-flight performance tasks (#738)."""

from __future__ import annotations

import asyncio

import pytest

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_poison_base_performance(monkeypatch):
    monkeypatch.setattr(api_module, "db_manager", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_CACHE", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_IN_FLIGHT", {})
    release = asyncio.Event()
    calls: list[int] = []

    async def slow_heavy(_func, _rows):
        calls.append(1)
        await release.wait()
        return {"S1": {"outcome": {}, "breakdown": None}}

    monkeypatch.setattr(analysis, "_run_heavy", slow_heavy)

    first = asyncio.create_task(analysis._base_performance(None, [], "fill-1", "S1"))
    await asyncio.sleep(0.01)
    first.cancel()  # what the refresh cap or a client disconnect does
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(analysis._base_performance(None, [], "fill-1", "S1"))
    await asyncio.sleep(0.01)
    release.set()
    result = await asyncio.wait_for(second, timeout=2)

    assert result["S1"] == {"outcome": {}, "breakdown": None}
    assert (
        len(calls) == 1
    )  # the shared task survived the cancelled waiter and was reused
    assert analysis._BASE_PERFORMANCE_IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_cancelled_in_flight_task_is_never_reused(monkeypatch):
    monkeypatch.setattr(api_module, "db_manager", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_CACHE", None)

    async def never():
        await asyncio.Event().wait()

    poisoned = asyncio.create_task(never())
    poisoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poisoned
    version = (id(None), "fill-1", "S1")
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_IN_FLIGHT", {version: poisoned})

    async def heavy(_func, _rows):
        return {"S1": {"outcome": {"ok": True}, "breakdown": None}}

    monkeypatch.setattr(analysis, "_run_heavy", heavy)

    result = await asyncio.wait_for(
        analysis._base_performance(None, [], "fill-1", "S1"), timeout=2
    )
    assert result["S1"]["outcome"] == {"ok": True}


@pytest.mark.asyncio
async def test_first_waiter_timing_out_never_cancels_the_shared_task(monkeypatch):
    monkeypatch.setattr(api_module, "db_manager", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_CACHE", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_IN_FLIGHT", {})
    release = asyncio.Event()

    async def slow_heavy(_func, _rows):
        await release.wait()
        return {"S1": {"outcome": {"n": 1}, "breakdown": None}}

    monkeypatch.setattr(analysis, "_run_heavy", slow_heavy)

    async def first_waiter():
        async with asyncio.timeout(0.02):  # the refresh cap firing
            return await analysis._base_performance(None, [], "fill-1", "S1")

    second = asyncio.create_task(analysis._base_performance(None, [], "fill-1", "S1"))
    with pytest.raises(TimeoutError):
        await first_waiter()
    release.set()
    result = await asyncio.wait_for(second, timeout=2)
    assert result["S1"]["outcome"] == {"n": 1}


@pytest.mark.asyncio
async def test_failed_shared_task_is_evicted_and_rebuilt(monkeypatch):
    monkeypatch.setattr(api_module, "db_manager", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_CACHE", None)
    monkeypatch.setattr(analysis, "_BASE_PERFORMANCE_IN_FLIGHT", {})
    attempts: list[int] = []

    async def flaky_heavy(_func, _rows):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("replay failed")
        return {"S1": {"outcome": {"n": 2}, "breakdown": None}}

    monkeypatch.setattr(analysis, "_run_heavy", flaky_heavy)
    with pytest.raises(RuntimeError):
        await analysis._base_performance(None, [], "fill-1", "S1")
    assert analysis._BASE_PERFORMANCE_IN_FLIGHT == {}
    result = await analysis._base_performance(None, [], "fill-1", "S1")
    assert result["S1"]["outcome"] == {"n": 2}
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_synthetic_exits_task_evicted_when_cancelled(monkeypatch):
    cache: dict = {}

    async def never():
        await asyncio.Event().wait()

    task = asyncio.create_task(never())
    cache["v"] = task
    analysis._evict_when_failed(cache, "v", task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)  # let the done callback run
    assert cache == {}
