"""Low-cardinality write metrics and five-minute summary logging."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from collections import Counter
from collections.abc import Callable
from threading import Lock
from typing import Any

from prometheus_client import (
    Counter as PrometheusCounter,
    Histogram,
)

logger = logging.getLogger(__name__)

PETROSA_DATA_MANAGER_WRITES_TOTAL = PrometheusCounter(
    "petrosa_data_manager_writes_total",
    "Data-manager writes by collection, outcome, and operation",
    ("collection", "outcome", "operation"),
)
PETROSA_DATA_MANAGER_WRITE_DURATION_SECONDS = Histogram(
    "petrosa_data_manager_write_duration_seconds",
    "Data-manager write duration in seconds",
    ("collection", "operation"),
)

_OUTCOMES = frozenset({"success", "duplicate", "failed", "skipped"})


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 6)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(percentile * 100) - 1], 6)


class WriteSummary:
    """Bounded process-local aggregation for the five-minute summary record."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._writes: Counter[str] = Counter()
        self._leases: Counter[str] = Counter()
        self._latencies: list[float] = []
        self._lock = Lock()

    def write(self, collection: str, outcome: str, operation: str, duration: float) -> None:
        with self._lock:
            self._writes[f"{collection}:{outcome}:{operation}"] += 1
            self._latencies.append(max(0.0, duration))

    def lease(self, result: str) -> None:
        with self._lock:
            self._leases[result] += 1

    def emit(self, *, force: bool = False) -> dict[str, Any] | None:
        with self._lock:
            if not force and self._clock() - self._started < 300:
                return None
            latencies = sorted(self._latencies)
            summary: dict[str, Any] = {
                "event": "SUMMARY",
                "window_seconds": 300,
                "service": "petrosa-data-manager",
                "writes": dict(self._writes),
                "lease_ops": dict(self._leases),
                "latency_seconds": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                },
            }
            self._started = self._clock()
            self._writes.clear()
            self._leases.clear()
            self._latencies.clear()
        logger.info(json.dumps(summary, separators=(",", ":")))
        return summary


SUMMARY = WriteSummary()


async def summary_loop(stop_event: asyncio.Event) -> None:
    """Emit healthy summaries at the five-minute boundary."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300)
        except TimeoutError:
            SUMMARY.emit()
        else:
            SUMMARY.emit(force=True)


def record_write(
    collection: str,
    outcome: str,
    operation: str,
    duration_seconds: float,
) -> None:
    """Record a bounded write outcome and duration."""
    if outcome not in _OUTCOMES:
        raise ValueError(f"unsupported write outcome: {outcome}")
    labels = {"collection": collection, "outcome": outcome, "operation": operation}
    PETROSA_DATA_MANAGER_WRITES_TOTAL.labels(**labels).inc()
    PETROSA_DATA_MANAGER_WRITE_DURATION_SECONDS.labels(
        collection=collection, operation=operation
    ).observe(max(0.0, duration_seconds))
    SUMMARY.write(collection, outcome, operation, duration_seconds)


__all__ = [
    "PETROSA_DATA_MANAGER_WRITE_DURATION_SECONDS",
    "PETROSA_DATA_MANAGER_WRITES_TOTAL",
    "SUMMARY",
    "WriteSummary",
    "record_write",
    "summary_loop",
]
