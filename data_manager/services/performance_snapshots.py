"""Per-pod strategy-performance snapshots, refreshed off the request path.

``GET /analysis/performance/{strategy_id}`` used to read every fill and replay it inside the request. With a
database round trip of about half a second, that cannot answer within the CIO's budget. This store keeps the
last good response per strategy in process memory. A background loop recomputes each strategy that was asked
for, so a request is a dictionary read.

The store lives in memory, not in MongoDB, because a MongoDB read on the request path costs the same round
trip this module removes. Each replica refreshes the strategies its own requests named.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from data_manager.services.report_precompute import record_report_stage

logger = logging.getLogger(__name__)

INTERVAL_ENV = "DATA_MANAGER_PERFORMANCE_SNAPSHOT_INTERVAL_SECONDS"
#: Fallback only, used when the env var is unset or invalid: one refresh per minute per requested strategy.
INTERVAL_FALLBACK_SECONDS = 60.0
#: A snapshot older than this many refresh intervals is not served (the convention of ``ReportPrecomputer``).
STALE_AFTER_INTERVALS = 3
#: A strategy nobody asked for during this many refresh intervals is no longer refreshed.
IDLE_AFTER_INTERVALS = 60

#: True while the background refresh computes a snapshot. The performance route reads it to use the full
#: dependency budget there instead of the short request-path deadlines.
snapshot_refresh_active: ContextVar[bool] = ContextVar(
    "performance_snapshot_refresh_active", default=False
)

Compute = Callable[[str], Awaitable[dict[str, Any]]]


def interval_seconds() -> float:
    """Return the configured refresh interval, or the labelled fallback."""
    configured = os.getenv(INTERVAL_ENV)
    if configured is None:
        return INTERVAL_FALLBACK_SECONDS
    try:
        value = float(configured)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(
            "performance_snapshot: invalid %s=%r; using fallback %.0fs",
            INTERVAL_ENV,
            configured,
            INTERVAL_FALLBACK_SECONDS,
        )
        return INTERVAL_FALLBACK_SECONDS
    return value


def is_good(body: dict[str, Any]) -> bool:
    """A body worth serving again: computed from history, not a degraded or no-history placeholder."""
    metadata = body.get("metadata") or {}
    return (
        not body.get("degraded")
        and not metadata.get("degraded")
        and metadata.get("history_status") != "unavailable"
    )


@dataclass
class _Snapshot:
    body: dict[str, Any]
    computed_at: datetime
    computed_monotonic: float


class PerformanceSnapshotStore:
    """The last good performance response per strategy, and which strategies to refresh."""

    def __init__(self, interval: float | None = None) -> None:
        self.interval = interval if interval is not None else interval_seconds()
        self._snapshots: dict[str, _Snapshot] = {}
        self._requested: dict[str, float] = {}

    def note_request(self, strategy_id: str) -> None:
        self._requested[strategy_id] = monotonic()

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        """Return a copy of the snapshot with its age, or ``None`` when absent or stale."""
        snapshot = self._snapshots.get(strategy_id)
        if snapshot is None:
            return None
        age = monotonic() - snapshot.computed_monotonic
        if age > self.interval * STALE_AFTER_INTERVALS:
            return None
        body = deepcopy(snapshot.body)
        metadata = body.setdefault("metadata", {})
        metadata["snapshot_status"] = "served"
        metadata["snapshot_computed_at"] = snapshot.computed_at.isoformat()
        metadata["snapshot_age_seconds"] = round(age, 3)
        return body

    def put(self, strategy_id: str, body: dict[str, Any]) -> bool:
        """Keep ``body`` when it is good; a degraded body never replaces a good one."""
        if not is_good(body):
            return False
        self._snapshots[strategy_id] = _Snapshot(
            deepcopy(body), datetime.now(UTC), monotonic()
        )
        return True

    def wanted(self) -> list[str]:
        """Strategies requested within the idle horizon; older ones are forgotten."""
        horizon = monotonic() - self.interval * IDLE_AFTER_INTERVALS
        for strategy_id in [s for s, at in self._requested.items() if at < horizon]:
            self._requested.pop(strategy_id, None)
            self._snapshots.pop(strategy_id, None)
        return sorted(self._requested)

    async def refresh_once(self, compute: Compute) -> dict[str, str]:
        """Recompute every wanted strategy once, one at a time; return each outcome."""
        outcomes: dict[str, str] = {}
        for strategy_id in self.wanted():
            started = monotonic()
            token = snapshot_refresh_active.set(True)
            try:
                body = await asyncio.wait_for(compute(strategy_id), self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcomes[strategy_id] = "error"
                logger.warning(
                    "performance_snapshot: refresh outcome=error strategy=%s "
                    "duration=%.3fs exception=%s",
                    strategy_id,
                    monotonic() - started,
                    type(exc).__name__,
                )
                continue
            finally:
                snapshot_refresh_active.reset(token)
            kept = self.put(strategy_id, body)
            outcomes[strategy_id] = "stored" if kept else "kept_previous"
            duration = monotonic() - started
            record_report_stage(
                "performance", "snapshot_refresh", duration, source="refresh"
            )
            logger.info(
                "performance_snapshot: refresh outcome=%s strategy=%s duration=%.3fs",
                outcomes[strategy_id],
                strategy_id,
                duration,
            )
        return outcomes

    async def run(self, compute: Compute) -> None:
        """Refresh forever, one pass per interval, until cancelled."""
        while True:
            started = monotonic()
            try:
                await self.refresh_once(compute)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("performance_snapshot: refresh pass failed")
            await asyncio.sleep(max(0.0, self.interval - (monotonic() - started)))


#: The process-wide store used by the performance route and started by the API lifespan.
store = PerformanceSnapshotStore()
