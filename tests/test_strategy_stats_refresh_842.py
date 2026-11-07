"""Leader-only refresh of the exchange-truth strategy stats (petrosa-data-manager#842)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance import strategy_stats_refresh as refresh_module
from data_manager.maintenance.strategy_stats_refresh import StrategyStatsRefresh

pytestmark = pytest.mark.asyncio

GROUND_TRUTH = {
    "episodes": [
        {
            "status": "attributed",
            "strategy_id": "iceberg",
            "strategy_shares": {"iceberg": 1.0},
            "open_time_ms": 1_791_000_000_000,
            "close_time_ms": 1_791_086_400_000,
            "realized_pnl": -4.9,
            "commission": 0.1,
            "net_pnl": -5.0,
            "fill_ids": [1, 2],
        }
    ],
    "reconciliation": {"gap": 0.0},
    "unmatched_headline": {"episodes": 1},
    "metadata": {"window": {"start": "a", "end": "b"}, "source": "binance"},
}


class _Leader:
    def __init__(self, leader: bool) -> None:
        self.is_leader = leader
        self.heartbeat_interval = 0.01


def _refresh(leader=True, compute=None):
    store = MagicMock()
    store.save = AsyncMock()
    active: list[bool] = []
    job = StrategyStatsRefresh(
        db_manager=MagicMock(),
        leader_election=_Leader(leader),
        on_active=active.append,
        compute=compute or AsyncMock(return_value=GROUND_TRUTH),
        store=store,
    )
    return job, store, active


async def test_run_once_stores_a_rule_a_snapshot():
    job, store, _ = _refresh()
    snapshot = await job.run_once()
    store.save.assert_awaited_once()
    saved = store.save.await_args.args[0]
    assert saved is snapshot
    assert isinstance(saved["computed_at"], datetime)  # the TTL field
    assert saved["window_days"] == 30.0 and saved["rule"].startswith("A:")
    assert saved["reconciliation"] == {"gap": 0.0}
    perf = saved["strategies"]["iceberg"]["performance"]
    assert (perf["wins"], perf["losses"]) == (0, 1)
    job._compute.assert_awaited_once_with(job.db_manager, 30.0)


async def test_run_once_is_bounded_by_the_run_budget(monkeypatch):
    monkeypatch.setattr(refresh_module, "RUN_BUDGET_SECONDS", 0.05)

    async def slow(*_args):
        await asyncio.sleep(5)

    job, store, _ = _refresh(compute=slow)
    with pytest.raises(TimeoutError):
        await job.run_once()
    store.save.assert_not_awaited()  # nothing partial is stored


async def _run_briefly(job, seconds=0.2):
    task = asyncio.create_task(job.start())
    await asyncio.sleep(seconds)
    await job.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_only_the_leader_refreshes_and_a_follower_waits(monkeypatch):
    monkeypatch.setattr(refresh_module, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(refresh_module.constants, "ENABLE_LEADER_ELECTION", True)
    follower, follower_store, follower_active = _refresh(leader=False)
    await _run_briefly(follower)
    follower_store.save.assert_not_awaited()
    assert follower_active and set(follower_active) == {
        False
    }  # reported 0, never active

    leader, leader_store, leader_active = _refresh(leader=True)
    await _run_briefly(leader)
    assert leader_store.save.await_count >= 1
    assert True in leader_active and leader_active[-1] is False  # active, then stopped


async def test_a_failed_run_is_counted_and_the_loop_keeps_going(monkeypatch):
    monkeypatch.setattr(refresh_module, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(refresh_module, "ERROR_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(refresh_module.constants, "ENABLE_LEADER_ELECTION", False)
    failing = AsyncMock(side_effect=RuntimeError("exchange history unavailable"))
    job, store, _ = _refresh(compute=failing)
    before = refresh_module.strategy_stats_refresh_total.labels(
        result="failure"
    )._value.get()
    await _run_briefly(job, seconds=0.1)
    after = refresh_module.strategy_stats_refresh_total.labels(
        result="failure"
    )._value.get()
    assert after - before >= 2  # retried after the backoff, the loop didn't die
    store.save.assert_not_awaited()


async def test_disabled_does_nothing(monkeypatch):
    monkeypatch.setenv("ENABLE_STRATEGY_STATS_REFRESH", "false")
    job, store, active = _refresh()
    await job.start()
    store.save.assert_not_awaited()
    assert active == []
