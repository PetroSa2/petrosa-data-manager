"""Per-pod strategy-performance snapshots, refreshed off the request path.

``GET /analysis/performance/{strategy_id}`` used to read every fill and replay it inside the request. With a
database round trip of about half a second, that cannot answer within the CIO's budget. This store keeps the
last good response per strategy in process memory. A background loop recomputes the configured strategies
(seeded from the strategy registry each pass) and any other strategy that was asked for, so a request is a
dictionary read and never a database round trip.

The store lives in memory, not in MongoDB, because a MongoDB read on the request path costs the same round
trip this module removes.
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

import constants
from data_manager.observability_metrics import (
    PERFORMANCE_SNAPSHOT_AGE,
    PERFORMANCE_SNAPSHOT_REFRESH,
)
from data_manager.services.report_precompute import record_report_stage

logger = logging.getLogger(__name__)

INTERVAL_ENV = "DATA_MANAGER_PERFORMANCE_SNAPSHOT_INTERVAL_SECONDS"
#: Fallback only, used when the env var is unset or invalid: one pass per minute. A pass that takes longer
#: than the interval is followed directly by the next one, so the measured pass duration sets the cadence then.
INTERVAL_FALLBACK_SECONDS = 60.0
#: A snapshot older than this many cadences is served flagged ``stale`` (the ``ReportPrecomputer`` convention).
STALE_AFTER_INTERVALS = 3
#: A snapshot older than this many cadences has expired: it is no longer served, because a caller that does
#: not read ``stale`` would act on it as current. The route answers degraded instead.
EXPIRE_AFTER_INTERVALS = 10
#: A strategy nobody asked for (and not in the registry) during this many cadences is no longer refreshed.
IDLE_AFTER_INTERVALS = 60
#: One compute has two sequential stages that are each bounded by the dependency budget: the
#: execution-events read and the replay. The cap per strategy is that many budgets.
COMPUTE_STAGES = 2

#: True while the background refresh computes a snapshot. The performance route reads it to give the
#: execution-events read the full dependency budget there instead of the short request-path deadline.
snapshot_refresh_active: ContextVar[bool] = ContextVar(
    "performance_snapshot_refresh_active", default=False
)

Compute = Callable[[str], Awaitable[dict[str, Any]]]
Seed = Callable[[], Awaitable[list[str]]]


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


def compute_cap_seconds() -> float:
    """The most one strategy's compute may take in the refresh."""
    return COMPUTE_STAGES * float(constants.ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS)


def _raise_if_cancelling() -> None:
    """Stop when this task was cancelled, even if an inner ``wait_for`` swallowed the CancelledError.

    On Python 3.11 ``asyncio.wait_for`` can drop a cancel that lands while its inner call finishes; the
    task's ``cancelling()`` count stays above zero, so checking it keeps the refresh loop stoppable.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


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
        self.last_pass_seconds = 0.0
        self._snapshots: dict[str, _Snapshot] = {}
        self._requested: dict[str, float] = {}

    @property
    def cadence(self) -> float:
        """Seconds between two refreshes of a strategy: the interval, or the last pass when it took longer."""
        return max(self.interval, self.last_pass_seconds)

    def note_request(self, strategy_id: str) -> None:
        self._requested[strategy_id] = monotonic()

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        """Return a copy of the snapshot with its age, or ``None`` when there is none yet.

        - Up to ``STALE_AFTER_INTERVALS`` cadences old: ``snapshot_status="served"``.
        - Up to ``EXPIRE_AFTER_INTERVALS`` cadences: served, flagged ``snapshot_status="stale"`` and ``stale``.
        - Older: only ``{"metadata": {...}}`` with ``snapshot_status="expired"``, the age and computed time,
          and no stats. The caller answers degraded.
        """
        snapshot = self._snapshots.get(strategy_id)
        if snapshot is None:
            return None
        age = monotonic() - snapshot.computed_monotonic
        snapshot_metadata = {
            "snapshot_computed_at": snapshot.computed_at.isoformat(),
            "snapshot_age_seconds": round(age, 3),
        }
        if age > self.cadence * EXPIRE_AFTER_INTERVALS:
            return {"metadata": {"snapshot_status": "expired", **snapshot_metadata}}
        stale = age > self.cadence * STALE_AFTER_INTERVALS
        body = deepcopy(snapshot.body)
        metadata = body.setdefault("metadata", {})
        metadata["snapshot_status"] = "stale" if stale else "served"
        metadata.update(snapshot_metadata)
        if stale:
            metadata["stale"] = True
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
        """Strategies requested or seeded within the idle horizon; older ones are forgotten."""
        horizon = monotonic() - self.cadence * IDLE_AFTER_INTERVALS
        for strategy_id in [s for s, at in self._requested.items() if at < horizon]:
            self._requested.pop(strategy_id, None)
            self._snapshots.pop(strategy_id, None)
        return sorted(self._requested)

    async def _seed(self, seed: Seed | None) -> str:
        """Register the registry's strategies; return ``none``, ``ok`` or ``error`` for the pass log."""
        if seed is None:
            return "none"
        try:
            async with asyncio.timeout(compute_cap_seconds()):
                strategy_ids = await seed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "performance_snapshot: seed outcome=error exception=%s",
                type(exc).__name__,
            )
            return "error"
        for strategy_id in strategy_ids:
            if strategy_id:
                self.note_request(str(strategy_id))
        return "ok"

    async def refresh_once(
        self, compute: Compute, seed: Seed | None = None
    ) -> dict[str, str]:
        """Recompute every wanted strategy once, one at a time; return each outcome."""
        pass_started = monotonic()
        seed_status = await self._seed(seed)
        outcomes: dict[str, str] = {}
        cap = compute_cap_seconds()
        for strategy_id in self.wanted():
            _raise_if_cancelling()
            started = monotonic()
            token = snapshot_refresh_active.set(True)
            try:
                # asyncio.timeout, not wait_for: on Python 3.11 wait_for can swallow an
                # outer cancel when the inner call finishes at the same moment, and the
                # loop then never stops (petrosa-data-manager#738).
                async with asyncio.timeout(cap):
                    body = await compute(strategy_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcomes[strategy_id] = "error"
                PERFORMANCE_SNAPSHOT_REFRESH.add(
                    1, {"strategy_id": strategy_id, "outcome": "error"}
                )
                logger.warning(
                    "performance_snapshot: refresh outcome=error strategy=%s "
                    "duration=%.3fs cap=%.1fs exception=%s",
                    strategy_id,
                    monotonic() - started,
                    cap,
                    type(exc).__name__,
                )
                continue
            finally:
                snapshot_refresh_active.reset(token)
            _raise_if_cancelling()
            kept = self.put(strategy_id, body)
            outcomes[strategy_id] = "stored" if kept else "kept_previous"
            PERFORMANCE_SNAPSHOT_REFRESH.add(
                1, {"strategy_id": strategy_id, "outcome": outcomes[strategy_id]}
            )
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
        self.last_pass_seconds = monotonic() - pass_started
        now = monotonic()
        for strategy_id, snapshot in self._snapshots.items():
            PERFORMANCE_SNAPSHOT_AGE.set(
                now - snapshot.computed_monotonic, {"strategy_id": strategy_id}
            )
        counts = {
            outcome: list(outcomes.values()).count(outcome)
            for outcome in ("stored", "kept_previous", "error")
        }
        logger.info(
            "performance_snapshot: pass seed=%s strategies=%d duration=%.3fs "
            "cadence=%.1fs stored=%d kept_previous=%d error=%d",
            seed_status,
            len(outcomes),
            self.last_pass_seconds,
            self.cadence,
            counts["stored"],
            counts["kept_previous"],
            counts["error"],
        )
        return outcomes

    async def run(self, compute: Compute, seed: Seed | None = None) -> None:
        """Refresh until cancelled; a pass starts one interval after the previous one started, never overlapping."""
        while True:
            _raise_if_cancelling()
            started = monotonic()
            try:
                await self.refresh_once(compute, seed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("performance_snapshot: refresh pass failed")
            await asyncio.sleep(max(0.0, self.interval - (monotonic() - started)))


#: The process-wide store used by the performance route and started by the API lifespan.
store = PerformanceSnapshotStore()
