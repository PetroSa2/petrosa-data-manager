"""The API lifespan runs the strategy-performance snapshot refresh (petrosa-data-manager#738)."""

from __future__ import annotations

import asyncio

import pytest

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis
import data_manager.api.routes.trading_state as trading_state
from data_manager.services import performance_snapshots as ps


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_snapshot_refresh(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    seen: list[object] = []

    async def fake_run(compute):
        seen.append(compute)
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

    assert seen == [analysis.compute_strategy_performance]
    assert cancelled.is_set()
