"""The API lifespan runs the strategy-performance snapshot refresh (petrosa-data-manager#738)."""

from __future__ import annotations

import asyncio

import pytest

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis
import data_manager.api.routes.trading_state as trading_state
from data_manager.services import performance_snapshots as ps


@pytest.mark.asyncio
async def test_lifespan_runs_the_refresh_under_the_supervisor(monkeypatch):
    seen: list[tuple] = []
    warm_hooks: list = []  # dm#878: the netting refresh is told when it is only a warm fallback

    async def fake_supervise(store, compute, seed, warm_only=None):
        seen.append((store, compute, seed))
        warm_hooks.append(warm_only)
        await asyncio.Event().wait()

    async def idle_worker():
        await asyncio.Event().wait()

    monkeypatch.setattr(trading_state, "mysql_copy_worker", idle_worker)
    monkeypatch.setattr(ps, "supervise", fake_supervise)

    async with api_module.lifespan(None):
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)

    assert seen == [
        (
            ps.store,
            analysis.compute_strategy_performance,
            api_module._configured_strategy_ids,
        )
    ]
    assert len(warm_hooks) == 1 and callable(warm_hooks[0])


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_snapshot_refresh(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    seen: list[object] = []

    async def fake_run(compute, seed, warm_only=None):
        seen.append((compute, seed))
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def idle_worker():
        await asyncio.Event().wait()

    monkeypatch.setattr(trading_state, "mysql_copy_worker", idle_worker)
    monkeypatch.setattr(ps.store, "run", fake_run)

    async with api_module.lifespan(None):
        await asyncio.wait_for(started.wait(), timeout=2)

    assert seen == [
        (analysis.compute_strategy_performance, api_module._configured_strategy_ids)
    ]
    assert cancelled.is_set()


class _Adapter:
    def __init__(self, config=None):
        self.config = config

    async def list_all_strategy_ids(self):
        return ["b", "a"]

    async def get_app_config(self):
        return self.config


def _use(monkeypatch, adapter, rows=None, events_error=None):
    async def load_events(mongodb, strategy_id=None, **_):
        assert mongodb is adapter and strategy_id is None  # source-wide, shared cache
        if events_error is not None:
            raise events_error
        return rows or [], "latest"

    monkeypatch.setattr(analysis, "_load_execution_events", load_events)
    monkeypatch.setattr(
        api_module, "db_manager", type("M", (), {"mongodb_adapter": adapter})()
    )


@pytest.mark.asyncio
async def test_seed_reads_strategy_registry(monkeypatch):
    monkeypatch.setattr(api_module, "db_manager", None)
    assert await api_module._configured_strategy_ids() == []
    _use(monkeypatch, _Adapter())
    assert await api_module._configured_strategy_ids() == ["b", "a"]


@pytest.mark.asyncio
async def test_seed_adds_allowlist_and_strategies_that_only_appear_in_fills(
    monkeypatch,
):
    adapter = _Adapter({"parameters": {"enabled_strategies": ["a", "allowed"]}})
    rows = [
        {"strategy_id": "spread_liquidity"},
        {"strategy_id": "b"},
        {"strategy_id": None},
        {"strategy_id": "spread_liquidity"},
    ]
    _use(monkeypatch, adapter, rows)

    assert await api_module._configured_strategy_ids() == [
        "b",
        "a",
        "allowed",
        "spread_liquidity",
    ]


@pytest.mark.asyncio
async def test_a_failing_extra_source_still_seeds_the_rest(monkeypatch, caplog):
    class BrokenConfig(_Adapter):
        async def get_app_config(self):
            raise RuntimeError("config down")

    _use(monkeypatch, BrokenConfig(), [{"strategy_id": "iceberg_detector"}])
    assert await api_module._configured_strategy_ids() == ["b", "a", "iceberg_detector"]
    assert "seed source=allowlist outcome=error exception=RuntimeError" in caplog.text

    _use(monkeypatch, _Adapter(), events_error=TimeoutError())
    assert await api_module._configured_strategy_ids() == ["b", "a"]
    assert (
        "seed source=execution_events outcome=error exception=TimeoutError"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_seed_never_swallows_a_cancel(monkeypatch):
    _use(monkeypatch, _Adapter(), events_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await api_module._configured_strategy_ids()
