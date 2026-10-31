"""Coverage for ``DataManagerApp._run_mongo_data_size_loop`` (data-manager#248).

The periodic MongoDB data-size gauge loop is the in-process driver for the
Atlas M0 leading-indicator producer. These tests exercise the method directly
(not just via inspect) so codecov sees the guard branches, one successful
refresh iteration, and the defense-in-depth exception path.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

import constants
from data_manager.main import DataManagerApp
from data_manager.maintenance import mongo_data_size_gauge as mdsg


def _app_with_adapter() -> DataManagerApp:
    app = DataManagerApp()
    # Minimal db_manager exposing a truthy mongodb_adapter.
    app.db_manager = types.SimpleNamespace(mongodb_adapter=object())
    return app


@pytest.mark.asyncio
async def test_loop_returns_early_when_disabled(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", False)
    app = _app_with_adapter()
    app.running = True

    # Must return immediately without touching the adapter.
    called = AsyncMock()
    monkeypatch.setattr(mdsg, "refresh_mongo_data_size", called)

    await app._run_mongo_data_size_loop()

    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_waits_for_adapter_then_refreshes(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = DataManagerApp()
    app.db_manager = None  # no adapter available
    app.running = False

    adapter = types.SimpleNamespace(
        db_name="delayed_adapter_db",
        db=types.SimpleNamespace(
            command=AsyncMock(return_value={"dataSize": 1234}),
        ),
        client=None,
    )

    actual_refresh = mdsg.refresh_mongo_data_size

    async def _refresh(_adapter, **_kwargs):
        app._shutdown_event.set()
        return await actual_refresh(_adapter, **_kwargs)

    refresh_mock = AsyncMock(side_effect=_refresh)
    monkeypatch.setattr(mdsg, "refresh_mongo_data_size", refresh_mock)

    async def _sleep(_interval):
        app.db_manager = types.SimpleNamespace(mongodb_adapter=adapter)

    monkeypatch.setattr("data_manager.main.asyncio.sleep", _sleep)

    await app._run_mongo_data_size_loop()

    refresh_mock.assert_awaited_once()
    assert refresh_mock.await_args.args[0] is adapter
    assert mdsg._last_data_size["delayed_adapter_db"] == 1234


@pytest.mark.asyncio
async def test_loop_stops_while_waiting_when_shutdown_is_signaled(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = DataManagerApp()
    app.running = False
    app.db_manager = None

    sleep_started = asyncio.Event()

    async def _sleep(_interval):
        sleep_started.set()
        app._shutdown_event.set()

    monkeypatch.setattr("data_manager.main.asyncio.sleep", _sleep)

    await app._run_mongo_data_size_loop()

    assert sleep_started.is_set()


@pytest.mark.asyncio
async def test_loop_runs_one_iteration_then_exits(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = _app_with_adapter()
    app.running = True

    # refresh signals shutdown after the first pass so the loop exits
    # deterministically.
    async def _refresh(adapter, **kwargs):
        app._shutdown_event.set()
        return {"petrosa_data_manager": 1}

    refresh_mock = AsyncMock(side_effect=_refresh)
    monkeypatch.setattr(mdsg, "refresh_mongo_data_size", refresh_mock)
    # Neutralize the inter-iteration sleep so the test is instant.
    monkeypatch.setattr("data_manager.main.asyncio.sleep", AsyncMock())

    await app._run_mongo_data_size_loop()

    refresh_mock.assert_awaited_once()
    # The adapter handed to refresh is the one on db_manager.
    assert refresh_mock.await_args.args[0] is app.db_manager.mongodb_adapter


@pytest.mark.asyncio
async def test_loop_isolates_unexpected_refresh_error(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = _app_with_adapter()
    app.running = True

    # refresh raises (defense-in-depth branch); loop must not propagate and
    # must still terminate after signaling shutdown.
    async def _boom(adapter, **kwargs):
        app._shutdown_event.set()
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mdsg, "refresh_mongo_data_size", AsyncMock(side_effect=_boom))
    monkeypatch.setattr("data_manager.main.asyncio.sleep", AsyncMock())

    # Must not raise.
    await app._run_mongo_data_size_loop()


def test_start_mongo_data_size_loop_is_disabled(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", False)
    app = DataManagerApp()

    app._start_mongo_data_size_loop()

    assert app.mongo_data_size_task is None


@pytest.mark.asyncio
async def test_start_mongo_data_size_loop_is_idempotent(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = DataManagerApp()
    app.running = False

    app._start_mongo_data_size_loop()
    first_task = app.mongo_data_size_task
    app._start_mongo_data_size_loop()

    assert first_task is not None
    assert app.mongo_data_size_task is first_task
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
