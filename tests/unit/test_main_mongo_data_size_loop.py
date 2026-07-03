"""Coverage for ``DataManagerApp._run_mongo_data_size_loop`` (data-manager#248).

The periodic MongoDB data-size gauge loop is the in-process driver for the
Atlas M0 leading-indicator producer. These tests exercise the method directly
(not just via inspect) so codecov sees the guard branches, one successful
refresh iteration, and the defense-in-depth exception path.
"""

from __future__ import annotations

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
async def test_loop_returns_early_when_no_adapter(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = DataManagerApp()
    app.db_manager = None  # no adapter available
    app.running = True

    called = AsyncMock()
    monkeypatch.setattr(mdsg, "refresh_mongo_data_size", called)

    await app._run_mongo_data_size_loop()

    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_runs_one_iteration_then_exits(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_MONGO_DATA_SIZE_GAUGE", True)
    app = _app_with_adapter()
    app.running = True

    # refresh stops the loop after the first pass so `while self.running`
    # exits deterministically.
    async def _refresh(adapter, **kwargs):
        app.running = False
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
    # must still terminate — we flip running off before raising.
    async def _boom(adapter, **kwargs):
        app.running = False
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mdsg, "refresh_mongo_data_size", AsyncMock(side_effect=_boom))
    monkeypatch.setattr("data_manager.main.asyncio.sleep", AsyncMock())

    # Must not raise.
    await app._run_mongo_data_size_loop()
