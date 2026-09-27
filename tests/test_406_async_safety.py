"""Regression coverage for the remaining #406 concurrency hazards."""

import asyncio
import threading
from unittest.mock import Mock

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes import generic


@pytest.mark.asyncio
async def test_signal_dual_write_drops_when_bound_is_full(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    adapter = Mock()

    def blocked_write(*_args):
        started.set()
        release.wait(timeout=2)

    adapter.write.side_effect = blocked_write
    monkeypatch.setattr(api_module, "db_manager", Mock(mysql_adapter=adapter))
    monkeypatch.setattr(
        generic, "_signal_dual_write_slots", threading.BoundedSemaphore(2)
    )
    before = generic.SIGNAL_DUAL_WRITE_DROPPED._value.get()

    generic._schedule_signals_mysql_copy([{"symbol": "BTCUSDT"}])
    generic._schedule_signals_mysql_copy([{"symbol": "ETHUSDT"}])
    await asyncio.sleep(0.05)
    assert started.is_set()

    generic._schedule_signals_mysql_copy([{"symbol": "SOLUSDT"}])
    await asyncio.sleep(0)
    assert generic.SIGNAL_DUAL_WRITE_DROPPED._value.get() == before + 1
    assert adapter.write.call_count <= 2

    release.set()
    await asyncio.gather(*list(generic._signal_dual_write_tasks))
