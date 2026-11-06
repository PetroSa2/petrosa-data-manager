"""Watchdog for the OTLP metric export (petrosa-data-manager#852).

Data-manager's OTLP metrics can stop silently: the SDK's
``PeriodicExportingMetricReader`` stops calling its exporter, nothing is exported and
nothing is logged, while traces and logs keep flowing. This module makes that visible
and diagnosable without exec-ing into a pod:

- every successful ``export()`` of each periodic metric reader is timestamped (the
  exporter's ``export`` is wrapped in place; behaviour is unchanged);
- a daemon thread, independent of the asyncio loop (which may itself be lagging),
  checks every interval. When no export has succeeded for ``STALL_INTERVALS`` export
  intervals it logs a WARNING, bumps
  ``data_manager_otel_metric_export_stalls_total`` on the prometheus_client registry
  (scraped on :9090, so it doesn't depend on the stalled OTLP path), and dumps every
  thread's stack once per stall to stderr with :mod:`faulthandler`. The stack shows
  where the reader thread is stuck.

Read-only with respect to the application: it never restarts or replaces the reader.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from prometheus_client import Counter

logger = logging.getLogger(__name__)

# An export is "stalled" after this many export intervals without a success.
STALL_INTERVALS = 3

otel_metric_export_stalls_total = Counter(
    "data_manager_otel_metric_export_stalls_total",
    "OTLP metric export stalls detected (no successful export for 3 intervals)",
)


class ExportWatch:
    """Timestamps successful exports of one periodic metric reader."""

    def __init__(
        self, reader: Any, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.reader = reader
        self.clock = clock
        self.interval_s = (
            float(getattr(reader, "_export_interval_millis", 60_000)) / 1000.0
        )
        self.last_success = clock()
        self.stalled = False
        exporter = getattr(reader, "_exporter", None)
        if exporter is None:
            raise ValueError("metric reader has no exporter")
        original = exporter.export

        def export(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            # MetricExportResult.SUCCESS has value 0 in the SDK.
            if getattr(result, "value", result) == 0:
                self.last_success = self.clock()
            return result

        exporter.export = export

    def check(self) -> bool:
        """True on the first check that finds a new stall."""
        silent_for = self.clock() - self.last_success
        if silent_for < STALL_INTERVALS * self.interval_s:
            if self.stalled:
                logger.warning("otel metric export recovered after a stall")
            self.stalled = False
            return False
        if self.stalled:
            return False
        self.stalled = True
        return True


def periodic_readers(meter_provider: Any) -> list[Any]:
    """The provider's periodic exporting metric readers (SDK internals, read-only)."""
    config = getattr(meter_provider, "_sdk_config", None)
    readers = getattr(config, "metric_readers", None)
    if readers is None:
        readers = getattr(meter_provider, "_metric_readers", None)
    return [
        r
        for r in (readers or ())
        if hasattr(r, "_exporter") and hasattr(r, "_export_interval_millis")
    ]


def on_stall(watch: ExportWatch, stream: Any = None) -> None:
    """Report one stall: WARNING, counter, and every thread's stack."""
    otel_metric_export_stalls_total.inc()
    logger.warning(
        "otel metric export stalled: no successful export for %.0f s (%d intervals); "
        "dumping all thread stacks to stderr",
        watch.clock() - watch.last_success,
        STALL_INTERVALS,
    )
    faulthandler.dump_traceback(file=stream or sys.stderr, all_threads=True)


def start_export_watchdog(
    meter_provider: Any,
    stop: threading.Event | None = None,
    check_every_s: float | None = None,
) -> threading.Thread | None:
    """Wrap every periodic reader's exporter and start the daemon checker thread."""
    watches = []
    for reader in periodic_readers(meter_provider):
        try:
            watches.append(ExportWatch(reader))
        except ValueError:
            continue
    if not watches:
        logger.warning("otel export watchdog: no periodic metric reader found")
        return None
    stop = stop or threading.Event()
    period = check_every_s or min(w.interval_s for w in watches)

    def run() -> None:
        while not stop.wait(period):
            for watch in watches:
                try:
                    if watch.check():
                        on_stall(watch)
                except Exception as exc:  # the watchdog must never die
                    logger.error("otel export watchdog check failed: %s", exc)

    thread = threading.Thread(target=run, name="OtelExportWatchdog", daemon=True)
    thread.start()
    return thread
