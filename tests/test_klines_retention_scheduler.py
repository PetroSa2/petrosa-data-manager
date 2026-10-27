import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance import klines_retention_scheduler as scheduler
from data_manager.maintenance.klines_retention import RetentionConfig


@pytest.mark.asyncio
async def test_shared_client_retention_retries_without_disconnect(monkeypatch):
    prune = AsyncMock(side_effect=[RuntimeError("temporary"), None])
    monkeypatch.setattr(scheduler, "prune_klines", prune)
    sleep = AsyncMock()
    adapter = MagicMock()
    config = RetentionConfig(mongo_retries=1, mongo_retry_backoff_seconds=2)

    await scheduler.run_shared_client_retention(adapter, config, sleep=sleep)

    assert prune.await_count == 2
    sleep.assert_awaited_once_with(2)
    adapter.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_shared_client_retention_propagates_terminal_failure(monkeypatch):
    prune = AsyncMock(side_effect=RuntimeError("terminal failure"))
    monkeypatch.setattr(scheduler, "prune_klines", prune)
    sleep = AsyncMock()
    adapter = MagicMock()
    config = RetentionConfig(mongo_retries=1, mongo_retry_backoff_seconds=2)

    with pytest.raises(RuntimeError, match="terminal failure"):
        await scheduler.run_shared_client_retention(adapter, config, sleep=sleep)

    assert prune.await_count == 2
    sleep.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_retention_loop_skips_followers(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(scheduler, "run_shared_client_retention", run)
    shutdown = asyncio.Event()

    async def stop_wait(_event, timeout):
        shutdown.set()
        raise TimeoutError

    monkeypatch.setattr(scheduler.asyncio, "wait_for", stop_wait)
    await scheduler.retention_loop(
        lambda: MagicMock(), shutdown, is_leader=lambda: False, interval_seconds=1
    )

    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_retention_loop_runs_for_leader_and_stops(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(scheduler, "run_shared_client_retention", run)
    shutdown = asyncio.Event()
    adapter = MagicMock(_connected=True)

    async def stop_wait(_event, timeout):
        shutdown.set()
        raise TimeoutError

    monkeypatch.setattr(scheduler.asyncio, "wait_for", stop_wait)
    await scheduler.retention_loop(
        lambda: adapter, shutdown, is_leader=lambda: True, interval_seconds=1
    )

    run.assert_awaited_once()
