import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.main import DataManagerApp


@pytest.mark.asyncio
async def test_database_retry_wires_manager_and_starts_consumers_once():
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    manager = MagicMock()
    manager.initialize = AsyncMock()

    with patch("data_manager.main.DatabaseManager", return_value=manager), patch(
        "data_manager.main.IntentConsumer"
    ) as intent, patch("data_manager.main.DecisionConsumer") as decision, patch(
        "data_manager.main.AlertDispatcher"
    ) as alerts, patch("data_manager.main.ExecutionEventsConsumer") as execution, patch(
        "data_manager.main.PnlConsumer"
    ) as pnl:
        for consumer in (intent, decision, alerts, execution, pnl):
            consumer.return_value.start = AsyncMock(return_value=True)
        with patch("data_manager.main.asyncio.sleep", new=AsyncMock()):
            await app._retry_database_initialization()
            await app._start_database_consumers_after_retry()
            await app._start_database_consumers_after_retry()

    assert app.db_manager is manager
    assert app._database_consumers_started
    assert intent.return_value.start.await_count == 1
    assert decision.return_value.start.await_count == 1
    assert execution.return_value.start.await_count == 1
    assert pnl.return_value.start.await_count == 1


@pytest.mark.asyncio
async def test_retry_backoff_is_capped_and_shutdown_cancels_task():
    app = DataManagerApp()
    app._db_retry_base_seconds = 2
    app._db_retry_cap_seconds = 60
    manager = MagicMock()
    manager.initialize = AsyncMock(side_effect=RuntimeError("offline"))
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            app._shutdown_event.set()

    with patch("data_manager.main.DatabaseManager", return_value=manager), patch(
        "data_manager.main.asyncio.sleep", new=sleep
    ):
        await app._retry_database_initialization()

    assert delays == [2, 4, 8, 16, 32]
    task = asyncio.create_task(asyncio.sleep(60))
    app._db_retry_task = task
    await app._cancel_database_retry()
    with pytest.raises(asyncio.CancelledError):
        await task
