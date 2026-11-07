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
    STARTUP_COMPONENT_UP,
)
from data_manager.services.report_precompute import record_report_stage

logger = logging.getLogger(__name__)

INTERVAL_ENV = "DATA_MANAGER_PERFORMANCE_SNAPSHOT_INTERVAL_SECONDS"
#: Fallback only, used when the env var is unset or invalid: one pass per minute. A pass that takes longer
#: than the interval is followed directly by the next one, so the measured pass duration sets the cadence then.
INTERVAL_FALLBACK_SECONDS = 60.0
#: dm#878: while the served stats source is ``ground_truth`` these netting snapshots are only a warm fallback,
#: so a pass runs at most this often instead of back to back (labelled; env-overridable).
WARM_FALLBACK_ENV = "DATA_MANAGER_PERFORMANCE_SNAPSHOT_WARM_FALLBACK_SECONDS"
WARM_FALLBACK_SECONDS = float(os.getenv(WARM_FALLBACK_ENV, "900"))
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


def _being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _raise_if_cancelling() -> None:
    """Stop when this task was cancelled, even if an inner ``wait_for`` swallowed the CancelledError.

    On Python 3.11 ``asyncio.wait_for`` can drop a cancel that lands while its inner call finishes; the
    task's ``cancelling()`` count stays above zero, so checking it keeps the refresh loop stoppable.
    """
    if _being_cancelled():
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
        # dm#878: True from the first slow (warm-fallback) pass until a normal pass completes, so the
        # staleness and expiry horizons keep the slow cadence across a flip back to netting.
        self.slow_pace = False
        self.last_pass_seconds = 0.0
        self._snapshots: dict[str, _Snapshot] = {}
        self._requested: dict[str, float] = {}

    @property
    def cadence(self) -> float:
        """Seconds between two refreshes of a strategy: the interval, or the last pass when it took longer."""
        pace = WARM_FALLBACK_SECONDS if self.slow_pace else 0.0
        return max(self.interval, self.last_pass_seconds, pace)

    def note_request(self, strategy_id: str) -> None:
        self._requested[strategy_id] = monotonic()

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        """Return a copy of the snapshot with its age, or ``None`` when there is none yet.

        - Served and stale bodies carry ``degraded=False`` at the top level and in ``metadata``.
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
        # Only good bodies are stored, so a served or stale answer is not degraded: say so explicitly,
        # because a caller testing ``degraded is False`` reads a missing key as not OK (#833).
        body["degraded"] = False
        metadata = body.setdefault("metadata", {})
        metadata["degraded"] = False
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
        """Register the seeded strategies; return ``none``, ``ok`` or ``error`` for the pass log.

        The seed runs as part of the refresh (``snapshot_refresh_active``), so the reads it shares with the
        computes get the refresh budget, not the request-path deadline.
        """
        if seed is None:
            return "none"
        token = snapshot_refresh_active.set(True)
        try:
            async with asyncio.timeout(compute_cap_seconds()):
                strategy_ids = await seed()
        except asyncio.CancelledError as exc:
            if _being_cancelled():
                raise
            logger.warning(
                "performance_snapshot: seed outcome=error exception=%s",
                type(exc).__name__,
            )
            return "error"
        except Exception as exc:
            logger.warning(
                "performance_snapshot: seed outcome=error exception=%s",
                type(exc).__name__,
            )
            return "error"
        finally:
            snapshot_refresh_active.reset(token)
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
            except asyncio.CancelledError as exc:
                if _being_cancelled():
                    raise
                # A CancelledError the compute raised on its own (e.g. a shared task another waiter
                # cancelled) is a failed refresh of this strategy, not a stop of the loop (#738).
                outcomes[strategy_id] = "error"
                PERFORMANCE_SNAPSHOT_REFRESH.add(
                    1, {"strategy_id": strategy_id, "outcome": "error"}
                )
                logger.warning(
                    "performance_snapshot: refresh outcome=error strategy=%s "
                    "duration=%.3fs exception=%s",
                    strategy_id,
                    monotonic() - started,
                    type(exc).__name__,
                )
                continue
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

    async def run(
        self,
        compute: Compute,
        seed: Seed | None = None,
        warm_only: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """Refresh until cancelled; a pass starts one interval after the previous one started, never overlapping.

        dm#878: ``warm_only()`` says whether these snapshots are only a fallback (the served stats source is
        ``ground_truth``). Then a pass runs at most every ``WARM_FALLBACK_SECONDS``; the check still happens
        every interval, so a flip back to netting gets a fresh pass within one interval, and until it
        completes the staleness horizons keep the slow cadence (no "expired" answer for an old snapshot).
        """
        last_pass: float | None = None
        while True:
            _raise_if_cancelling()
            started = monotonic()
            slow = False
            if warm_only is not None:
                try:
                    slow = bool(await warm_only())
                except Exception:
                    slow = False  # unknown source: keep the normal cadence
            due = (
                not slow
                or last_pass is None
                or started - last_pass >= WARM_FALLBACK_SECONDS
            )
            if due:
                if slow:
                    self.slow_pace = True
                try:
                    await self.refresh_once(compute, seed)
                    last_pass = started
                    if not slow:
                        self.slow_pace = False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("performance_snapshot: refresh pass failed")
            await asyncio.sleep(max(0.0, self.interval - (monotonic() - started)))


#: Restart backoff for the supervised loop: starts at one cadence, doubles, capped at this many cadences.
RESTART_BACKOFF_MAX_INTERVALS = 10


async def supervise(
    store: PerformanceSnapshotStore,
    compute: Compute,
    seed: Seed | None = None,
    warm_only: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Run the refresh loop and restart it whenever it ends for any reason other than shutdown.

    A loop that died silently left one pod serving expired snapshots for its whole lifetime (#738,
    2026-11-05). Every unexpected exit is an ERROR, sets ``startup_component_up{performance_snapshots}``
    to 0 and restarts after a backoff, so a future bug costs one pass, not the pod's lifetime.
    """
    backoff = store.interval
    while True:
        STARTUP_COMPONENT_UP.set(1, {"component": "performance_snapshots"})
        try:
            await store.run(compute, seed, warm_only)
            reason = "returned"
        except asyncio.CancelledError:
            if _being_cancelled():
                STARTUP_COMPONENT_UP.set(0, {"component": "performance_snapshots"})
                raise
            reason = "CancelledError"
        except Exception as exc:
            reason = type(exc).__name__
        STARTUP_COMPONENT_UP.set(0, {"component": "performance_snapshots"})
        logger.error(
            "performance_snapshot: refresh loop exited unexpectedly (%s); restarting in %.1fs",
            reason,
            backoff,
        )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, store.interval * RESTART_BACKOFF_MAX_INTERVALS)


#: The process-wide store used by the performance route and started by the API lifespan.
store = PerformanceSnapshotStore()
