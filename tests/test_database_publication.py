"""The API sees a database manager only once it is fully initialized (#829)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import data_manager.api.app as api_app


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(api_app, "db_manager", None)
    monkeypatch.setattr(api_app, "_database_ready", None)
    monkeypatch.setattr(api_app, "_database_ready_loop", None)


@pytest.mark.asyncio
async def test_waiter_blocks_until_published_then_gets_the_manager():
    manager = MagicMock()
    waiter = asyncio.create_task(api_app.wait_for_database())
    await asyncio.sleep(0.02)
    assert not waiter.done()

    api_app.publish_database(manager)

    assert await asyncio.wait_for(waiter, timeout=1) is manager
    assert api_app.db_manager is manager


@pytest.mark.asyncio
async def test_unpublish_blocks_new_waiters_again():
    first, second = MagicMock(name="first"), MagicMock(name="second")
    api_app.publish_database(first)
    assert await api_app.wait_for_database() is first

    api_app.unpublish_database(first)
    waiter = asyncio.create_task(api_app.wait_for_database())
    await asyncio.sleep(0.02)
    assert not waiter.done()
    api_app.publish_database(second)
    assert await asyncio.wait_for(waiter, timeout=1) is second


@pytest.mark.asyncio
async def test_unpublish_of_another_manager_is_a_no_op():
    current, stale = MagicMock(name="current"), MagicMock(name="stale")
    api_app.publish_database(current)
    api_app.unpublish_database(stale)
    assert api_app.db_manager is current


@pytest.mark.asyncio
async def test_waiter_is_cancellable_for_shutdown():
    waiter = asyncio.create_task(api_app.wait_for_database())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
