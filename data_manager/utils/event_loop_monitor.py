"""Opt-in asyncio event-loop lag diagnostics (petrosa-data-manager#370).

Two cooperating parts:

* A coroutine on the event loop sleeps ``interval_s`` in a loop and measures
  how late each wake-up is. Lateness at or above ``warning_threshold_ms`` is
  logged as ``EVENT_LOOP_LAG ms=<n>`` at WARNING once the loop runs again.
* A daemon watchdog thread watches the coroutine's heartbeat. When the
  heartbeat is overdue by ``warning_threshold_ms``, the loop is blocked right
  now, so the watchdog samples the loop thread's stack with
  ``sys._current_frames()`` and logs it, at most once per
  ``report_interval_s``.

The stack has to be sampled from another thread. When the coroutine itself
runs again, the blocking code has already returned, and the loop thread's
stack is only the monitor's own frames.
"""

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

_last_lag_ms: float | None = None
_last_lag_at: float | None = None

WATCHDOG_THREAD_NAME = "event-loop-lag-watchdog"
_STACK_LIMIT = 20


def event_loop_snapshot() -> dict[str, float | str | None]:
    """Return the latest event-loop state without touching the event loop."""
    if _last_lag_ms is None or _last_lag_at is None:
        return {"state": "unknown", "lag_ms": None}
    age_s = time.monotonic() - _last_lag_at
    if age_s > 120:
        return {"state": "stale", "lag_ms": _last_lag_ms}
    return {
        "state": "lagging" if _last_lag_ms >= 500 else "healthy",
        "lag_ms": _last_lag_ms,
    }


class LoopWatchdog:
    """Log the event-loop thread's stack while the loop is blocked."""

    def __init__(
        self,
        *,
        loop_thread_id: int,
        stall_after_s: float,
        report_interval_s: float,
        poll_s: float,
    ) -> None:
        self._loop_thread_id = loop_thread_id
        self._stall_after_s = stall_after_s
        self._report_interval_s = report_interval_s
        self._poll_s = poll_s
        self._last_beat = time.monotonic()
        self._last_report: float | None = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=WATCHDOG_THREAD_NAME, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def beat(self, now: float) -> None:
        """Record that the event loop ran the monitor coroutine at ``now``."""
        self._last_beat = now

    def check(self, now: float) -> bool:
        """Log the loop thread's stack if the heartbeat is overdue.

        Returns True when a stack was logged. Called from the watchdog
        thread; it is a separate method so tests can drive it directly.
        """
        stalled_s = now - self._last_beat
        if stalled_s < self._stall_after_s:
            return False
        if (
            self._last_report is not None
            and now - self._last_report < self._report_interval_s
        ):
            return False
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return False
        self._last_report = now
        stack = "".join(traceback.format_stack(frame, limit=_STACK_LIMIT))
        logger.warning(
            "EVENT_LOOP_LAG stack (loop blocked %.0f ms):\n%s",
            stalled_s * 1000,
            stack,
        )
        return True

    def _run(self) -> None:
        while not self._stopped.wait(self._poll_s):
            self.check(time.monotonic())


async def monitor_event_loop_lag(
    *,
    stop_event: asyncio.Event,
    interval_s: float = 0.5,
    warning_threshold_ms: float = 500.0,
    report_interval_s: float = 60.0,
) -> None:
    """Report scheduling pauses and what the loop thread was doing.

    The application leaves this off by default (``DM_LOOP_LAG_MONITOR``).
    ``stop_event`` makes shutdown deterministic in tests and at process
    termination; cancelling the task works too.
    """
    threshold_s = warning_threshold_ms / 1000
    watchdog = LoopWatchdog(
        loop_thread_id=threading.get_ident(),
        # Beats are interval_s apart when the loop is healthy.
        stall_after_s=interval_s + threshold_s,
        report_interval_s=report_interval_s,
        # Poll often enough to catch a stall while it is still in progress.
        poll_s=max(threshold_s / 5, 0.01),
    )
    watchdog.start()
    expected = time.monotonic() + interval_s
    try:
        while not stop_event.is_set():
            await asyncio.sleep(interval_s)
            now = time.monotonic()
            watchdog.beat(now)
            lag_ms = max(0.0, (now - expected) * 1000)
            expected = now + interval_s
            global _last_lag_at, _last_lag_ms
            _last_lag_at = now
            _last_lag_ms = lag_ms
            if lag_ms >= warning_threshold_ms:
                logger.warning("EVENT_LOOP_LAG ms=%.1f", lag_ms)
    finally:
        watchdog.stop()
