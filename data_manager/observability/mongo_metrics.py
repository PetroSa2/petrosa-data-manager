"""Bounded MongoDB command metrics."""

from __future__ import annotations

import contextvars
import json
import logging
import statistics
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Mapping

from opentelemetry import metrics as _otel_metrics
from pymongo.monitoring import CommandListener

try:
    from petrosa_otel import get_meter
except ImportError:  # pragma: no cover
    get_meter = _otel_metrics.get_meter

logger = logging.getLogger(__name__)
_source = contextvars.ContextVar("mongo_metrics_source", default=None)
_UNKNOWN = "unknown"

_meter = get_meter(__name__)
ATLAS_OPERATIONS = _meter.create_counter(
    "data_manager_atlas_operations_total",
    description="MongoDB Atlas commands completed",
)
ATLAS_FAILURES = _meter.create_counter(
    "data_manager_atlas_operation_failures_total",
    description="MongoDB Atlas commands that failed",
)
ATLAS_LATENCY = _meter.create_histogram(
    "data_manager_atlas_operation_duration_seconds",
    description="MongoDB Atlas command duration",
    unit="s",
)


def set_source(source: str):
    return _source.set(source or _UNKNOWN)


def reset_source(token) -> None:
    _source.reset(token)


def _label(value: object) -> str:
    return value if isinstance(value, str) and value else _UNKNOWN


def _collection(command: Mapping[str, object]) -> str:
    for key in ("collection", "find", "aggregate", "insert", "update", "delete"):
        value = command.get(key)
        if isinstance(value, str) and value:
            return value
    return _UNKNOWN


def metric_name(instrument: object) -> str:
    return getattr(instrument, "name", getattr(instrument, "_name", ""))


class AtlasCommandListener(CommandListener):
    """Record command events without affecting the monitored client."""

    def __init__(self, client_role: str):
        self.client_role = _label(client_role)
        self._commands: dict[int, tuple[str, str, str]] = {}
        self._lock = threading.Lock()

    def started(self, event) -> None:
        try:
            command = event.command
            source = _source.get() or self.client_role
            with self._lock:
                if len(self._commands) >= 4096:
                    self._commands.pop(next(iter(self._commands)))
                self._commands[event.request_id] = (
                    _label(event.command_name),
                    _collection(command),
                    _label(source),
                )
        except Exception:
            logger.debug("Mongo command metric start failed", exc_info=True)

    def _finish(self, event, failed: bool) -> None:
        try:
            with self._lock:
                labels = self._commands.pop(event.request_id, None)
            if labels is None:
                labels = (_label(event.command_name), _UNKNOWN, self.client_role)
            attributes = {
                "command": labels[0],
                "collection": labels[1],
                "source": labels[2],
            }
            ATLAS_OPERATIONS.add(1, attributes)
            if failed:
                ATLAS_FAILURES.add(1, attributes)
            ATLAS_LATENCY.record(event.duration_micros / 1_000_000, attributes)
            ATLAS_SUMMARY.record(
                attributes["collection"],
                attributes["command"],
                attributes["source"],
                event.duration_micros / 1_000_000,
                failed,
            )
        except Exception:
            logger.debug("Mongo command metric finish failed", exc_info=True)

    def succeeded(self, event) -> None:
        self._finish(event, False)

    def failed(self, event) -> None:
        self._finish(event, True)


class AtlasOperationSummary:
    """Bounded five-minute aggregation for read-only Atlas load evidence."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._operations: Counter[tuple[str, str, str]] = Counter()
        self._failures: Counter[tuple[str, str, str]] = Counter()
        self._latencies: defaultdict[tuple[str, str, str], list[float]] = defaultdict(
            list
        )
        self._minutes: Counter[int] = Counter()
        self._lock = threading.Lock()

    def record(
        self,
        collection: str,
        command: str,
        source: str,
        duration: float,
        failed: bool,
    ) -> None:
        key = (_label(collection), _label(command), _label(source))
        with self._lock:
            if len(self._operations) >= 512 and key not in self._operations:
                return
            self._operations[key] += 1
            if failed:
                self._failures[key] += 1
            samples = self._latencies[key]
            if len(samples) < 256:
                samples.append(max(0.0, duration))
            self._minutes[int((self._clock() - self._started) // 60)] += 1

    def emit(self, *, force: bool = False) -> dict[str, object] | None:
        with self._lock:
            elapsed = max(0.0, self._clock() - self._started)
            if not force and elapsed < 300:
                return None
            operations = self._top(self._operations)
            failures = self._top(self._failures)
            total = sum(self._operations.values())
            summary: dict[str, object] = {
                "event": "ATLAS_OPS_SUMMARY",
                "window_seconds": round(elapsed, 3),
                "service": "petrosa-data-manager",
                "top_operations": operations,
                "top_failures": failures,
                "total_ops": total,
                "total_ops_per_second": round(total / elapsed, 6) if elapsed else 0.0,
                "peak_minute_ops": max(self._minutes.values(), default=0),
                "peak_minute_ops_per_second": round(
                    max(self._minutes.values(), default=0) / 60, 6
                ),
            }
            self._started = self._clock()
            self._operations.clear()
            self._failures.clear()
            self._latencies.clear()
            self._minutes.clear()
        logger.info("ATLAS_OPS_SUMMARY %s", json.dumps(summary, separators=(",", ":")))
        return summary

    def _top(self, counts: Counter[tuple[str, str, str]]) -> list[dict[str, object]]:
        result = []
        for key, count in counts.most_common(10):
            collection, command, source = key
            latencies = self._latencies[key]
            result.append(
                {
                    "collection": collection,
                    "command": command,
                    "source": source,
                    "count": count,
                    "p95_latency_seconds": round(
                        statistics.quantiles(latencies, n=20, method="inclusive")[18], 6
                    )
                    if len(latencies) > 1
                    else round(latencies[0], 6)
                    if latencies
                    else 0.0,
                }
            )
        return result


ATLAS_SUMMARY = AtlasOperationSummary()


def emit_atlas_summary(*, force: bool = False) -> dict[str, object] | None:
    return ATLAS_SUMMARY.emit(force=force)
