"""Bounded MongoDB command metrics."""

from __future__ import annotations

import contextvars
import logging
import threading
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

    def command_started(self, event) -> None:
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
        except Exception:
            logger.debug("Mongo command metric finish failed", exc_info=True)

    def command_succeeded(self, event) -> None:
        self._finish(event, False)

    def command_failed(self, event) -> None:
        self._finish(event, True)
