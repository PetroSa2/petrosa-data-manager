import asyncio
import logging

import pytest

from data_manager.utils.event_loop_monitor import monitor_event_loop_lag


@pytest.mark.asyncio
async def test_monitor_is_quiet_when_loop_is_healthy(caplog):
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        monitor_event_loop_lag(
            stop_event=stop_event,
            interval_s=0.001,
            warning_threshold_ms=1000,
        )
    )
    await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert "EVENT_LOOP_LAG" not in caplog.text


@pytest.mark.asyncio
async def test_monitor_logs_lag_and_rate_limits_stack(caplog):
    stop_event = asyncio.Event()
    caplog.set_level(logging.WARNING)
    task = asyncio.create_task(
        monitor_event_loop_lag(
            stop_event=stop_event,
            interval_s=0.01,
            warning_threshold_ms=0,
            report_interval_s=60,
        )
    )
    await asyncio.sleep(0.03)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert any("EVENT_LOOP_LAG ms=" in record.message for record in caplog.records)
    assert (
        sum("EVENT_LOOP_LAG stack:" in record.message for record in caplog.records) <= 1
    )
