"""Opt-in asyncio event-loop lag diagnostics."""

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)


async def monitor_event_loop_lag(
    *,
    stop_event: asyncio.Event,
    interval_s: float = 0.5,
    warning_threshold_ms: float = 500.0,
    report_interval_s: float = 60.0,
) -> None:
    """Report scheduling pauses and the loop thread's current stack.

    The monitor is deliberately lightweight and disabled by default by the
    application.  ``stop_event`` makes shutdown deterministic in tests and at
    process termination.
    """
    last_report = 0.0
    expected = time.monotonic() + interval_s
    try:
        while not stop_event.is_set():
            await asyncio.sleep(interval_s)
            now = time.monotonic()
            lag_ms = max(0.0, (now - expected) * 1000)
            expected = now + interval_s
            if lag_ms < warning_threshold_ms:
                continue

            logger.warning("EVENT_LOOP_LAG ms=%.1f", lag_ms)
            if now - last_report < report_interval_s:
                continue
            last_report = now
            thread_id = threading.current_thread().ident
            frame = (
                sys._current_frames().get(thread_id) if thread_id is not None else None
            )
            if frame is not None:
                stack = "".join(traceback.format_stack(frame, limit=20))
                logger.warning("EVENT_LOOP_LAG stack:\n%s", stack)
    except asyncio.CancelledError:
        raise
