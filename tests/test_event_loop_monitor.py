"""Tests for the opt-in event-loop lag monitor (petrosa-data-manager#370)."""

import asyncio
import logging
import threading
import time

import pytest

import constants
import data_manager.main as main_module
from data_manager.main import DataManagerApp
from data_manager.utils.event_loop_monitor import (
    WATCHDOG_THREAD_NAME,
    LoopWatchdog,
    monitor_event_loop_lag,
)

MONITOR_LOGGER = "data_manager.utils.event_loop_monitor"


def _lag_records(caplog):
    return [
        r for r in caplog.records if r.getMessage().startswith("EVENT_LOOP_LAG ms=")
    ]


def _stack_records(caplog):
    return [
        r for r in caplog.records if r.getMessage().startswith("EVENT_LOOP_LAG stack")
    ]


def _watchdog_threads():
    return [t for t in threading.enumerate() if t.name == WATCHDOG_THREAD_NAME]


async def _wait_for_watchdogs_to_exit(timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while _watchdog_threads() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


async def _blocking_handler() -> None:
    """Simulates a coroutine that makes a synchronous call on the event loop."""
    time.sleep(1)


@pytest.mark.asyncio
async def test_monitor_is_quiet_when_loop_is_healthy(caplog):
    caplog.set_level(logging.WARNING, logger=MONITOR_LOGGER)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        monitor_event_loop_lag(
            stop_event=stop_event,
            interval_s=0.001,
            warning_threshold_ms=1000,
        )
    )
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert "EVENT_LOOP_LAG" not in caplog.text


@pytest.mark.asyncio
async def test_blocking_coroutine_emits_lag_warning_with_offending_stack(caplog):
    """AC1: with the monitor on, a coroutine doing time.sleep(1) is reported,
    and the stack names the blocking coroutine rather than the monitor."""
    caplog.set_level(logging.WARNING, logger=MONITOR_LOGGER)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        monitor_event_loop_lag(
            stop_event=stop_event, interval_s=0.05, warning_threshold_ms=500
        )
    )
    await asyncio.sleep(0.15)  # a few healthy ticks first
    await _blocking_handler()
    await asyncio.sleep(0.15)  # let the monitor wake up and measure the pause
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    lag = _lag_records(caplog)
    assert lag, caplog.text
    assert all(r.levelno == logging.WARNING for r in lag)
    worst_ms = max(float(r.getMessage().split("=", 1)[1]) for r in lag)
    assert worst_ms >= 500

    stacks = _stack_records(caplog)
    assert len(stacks) == 1, caplog.text
    assert stacks[0].levelno == logging.WARNING
    assert "_blocking_handler" in stacks[0].getMessage()
    assert "time.sleep(1)" in stacks[0].getMessage()


@pytest.mark.asyncio
async def test_stack_dump_is_rate_limited(caplog):
    caplog.set_level(logging.WARNING, logger=MONITOR_LOGGER)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        monitor_event_loop_lag(
            stop_event=stop_event,
            interval_s=0.02,
            warning_threshold_ms=150,
            report_interval_s=60,
        )
    )
    await asyncio.sleep(0.05)
    for _ in range(2):
        time.sleep(0.4)  # deliberately block the event loop
        await asyncio.sleep(0.1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(_lag_records(caplog)) >= 2, caplog.text
    assert len(_stack_records(caplog)) == 1, caplog.text


@pytest.mark.asyncio
async def test_watchdog_thread_stops_with_the_monitor():
    await _wait_for_watchdogs_to_exit()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        monitor_event_loop_lag(
            stop_event=stop_event, interval_s=0.01, warning_threshold_ms=1000
        )
    )
    await asyncio.sleep(0.05)
    assert len(_watchdog_threads()) == 1

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await _wait_for_watchdogs_to_exit()

    assert _watchdog_threads() == []


class TestLoopWatchdogCheck:
    """Drive LoopWatchdog.check() directly, without the background thread."""

    @staticmethod
    def _watchdog(thread_id: int) -> LoopWatchdog:
        watchdog = LoopWatchdog(
            loop_thread_id=thread_id,
            stall_after_s=1.0,
            report_interval_s=60.0,
            poll_s=0.1,
        )
        watchdog.beat(100.0)
        return watchdog

    def test_no_report_while_heartbeat_is_fresh(self, caplog):
        caplog.set_level(logging.WARNING, logger=MONITOR_LOGGER)
        watchdog = self._watchdog(threading.get_ident())

        assert watchdog.check(100.5) is False
        assert _stack_records(caplog) == []

    def test_reports_stack_of_the_stalled_thread_once_per_interval(self, caplog):
        caplog.set_level(logging.WARNING, logger=MONITOR_LOGGER)
        watchdog = self._watchdog(threading.get_ident())

        assert watchdog.check(101.5) is True
        assert watchdog.check(130.0) is False  # within report_interval_s
        assert watchdog.check(162.0) is True  # interval elapsed, still stalled

        stacks = _stack_records(caplog)
        assert len(stacks) == 2
        assert "loop blocked 1500 ms" in stacks[0].getMessage()
        assert "test_reports_stack_of_the_stalled_thread" in stacks[0].getMessage()

    def test_unknown_loop_thread_is_not_reported(self, caplog):
        caplog.set_level(logging.WARNING, logger=MONITOR_LOGGER)
        watchdog = self._watchdog(-1)  # no live thread has a negative ident

        assert watchdog.check(200.0) is False
        assert _stack_records(caplog) == []


def test_application_does_not_create_monitor_when_disabled(monkeypatch):
    app = DataManagerApp()
    monkeypatch.setattr(constants, "DM_LOOP_LAG_MONITOR", False)

    app._start_loop_lag_monitor()

    assert app.loop_lag_monitor_task is None


@pytest.mark.asyncio
async def test_application_creates_monitor_when_enabled(monkeypatch):
    app = DataManagerApp()
    monkeypatch.setattr(constants, "DM_LOOP_LAG_MONITOR", True)

    app._start_loop_lag_monitor()
    assert app.loop_lag_monitor_task is not None

    app.loop_lag_monitor_task.cancel()
    await asyncio.gather(app.loop_lag_monitor_task, return_exceptions=True)


class _StopStartup(BaseException):
    """Aborts DataManagerApp.start() past its ``except Exception`` handlers."""


@pytest.mark.asyncio
async def test_start_runs_monitor_during_database_init_and_stop_cancels_it(
    monkeypatch,
):
    """The monitor must already be running while databases initialize, since
    the MySQL connect in DatabaseManager.initialize() is one of the blockers
    it exists to catch. stop() must then cancel it."""
    monkeypatch.setattr(constants, "DM_LOOP_LAG_MONITOR", True)
    monkeypatch.setattr(constants, "ENABLE_API", False)
    monkeypatch.setattr(main_module, "start_http_server", lambda port: None)
    app = DataManagerApp()
    seen: dict[str, bool] = {}

    class FakeDatabaseManager:
        async def initialize(self) -> None:
            task = app.loop_lag_monitor_task
            seen["monitor_running"] = task is not None and not task.done()
            raise _StopStartup

        async def shutdown(self) -> None:
            seen["db_shutdown"] = True

    monkeypatch.setattr(main_module, "DatabaseManager", FakeDatabaseManager)

    with pytest.raises(_StopStartup):
        await app.start()
    assert seen["monitor_running"] is True
    monitor_task = app.loop_lag_monitor_task

    await app.stop()

    assert monitor_task is not None and monitor_task.cancelled()
    assert app.loop_lag_monitor_task is None
    assert seen["db_shutdown"] is True
