"""Leader-only refresh of the exchange-truth strategy stats (petrosa-data-manager#842).

Every ``REFRESH_INTERVAL_SECONDS`` (15 min) the leader pod recomputes the 30-day exchange ground truth
(:func:`compute_ground_truth`, petrosa-data-manager#844), turns it into per-strategy stats with rule A
(:func:`build_strategy_stats`) and stores one snapshot. The API serves the latest snapshot; nothing here changes
what CIO sees until the ``strategy_stats_source`` switch says ``ground_truth``.

Exchange weight: the reads go through tradeengine's history routes, which admit them under their own guard of
10 % of the account's per-minute weight (PetroSa2/petrosa-tradeengine#833), so trading's 90 % is never used by
this job. A run takes about 2 min.

Leadership follows the candle warm-up pattern (petrosa-data-manager#848/#849): the loop never exits on
leadership; a follower waits and re-checks every lease heartbeat, and ``on_active`` reports the state to the
``startup_component_up`` gauge. A cancel always propagates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge

import constants
from data_manager.services.strategy_stats_exchange import RULE, build_strategy_stats
from data_manager.services.strategy_stats_snapshot_store import (
    REFRESH_INTERVAL_SECONDS,
    StrategyStatsSnapshotStore,
)

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30.0
#: How often a follower re-checks leadership when the election manager gives no heartbeat interval.
LEADER_POLL_FALLBACK_SECONDS = 30.0
#: Labelled fallback: delay after startup before the first run, so warm-up finishes first.
INITIAL_DELAY_SECONDS = float(
    os.getenv("STRATEGY_STATS_REFRESH_INITIAL_DELAY_SECONDS", "120")
)
#: Labelled fallback: wait after a failed run before trying again.
ERROR_BACKOFF_SECONDS = float(
    os.getenv("STRATEGY_STATS_REFRESH_ERROR_BACKOFF_SECONDS", "300")
)
#: Policy ceiling for one run (the ground-truth route's time budget for 30 days is ~218 s).
RUN_BUDGET_SECONDS = float(
    os.getenv("STRATEGY_STATS_REFRESH_RUN_BUDGET_SECONDS", "600")
)

strategy_stats_refresh_total = Counter(
    "data_manager_strategy_stats_refresh_total",
    "Exchange-truth strategy stats refresh runs by result",
    ["result"],
)
strategy_stats_refresh_last_success = Gauge(
    "data_manager_strategy_stats_refresh_last_success_timestamp_seconds",
    "Unix time of the last stored exchange-truth stats snapshot",
)
strategy_stats_refresh_seconds = Gauge(
    "data_manager_strategy_stats_refresh_last_seconds",
    "Duration of the last exchange-truth stats refresh run in seconds",
)


def _enabled() -> bool:
    return os.getenv("ENABLE_STRATEGY_STATS_REFRESH", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }


class StrategyStatsRefresh:
    def __init__(
        self,
        db_manager: Any,
        leader_election: Any = None,
        on_active: Callable[[bool], None] | None = None,
        compute: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        store: StrategyStatsSnapshotStore | None = None,
    ) -> None:
        self.db_manager = db_manager
        self.leader_election = leader_election
        self.on_active = on_active
        self._compute = compute
        self._store = store
        self.running = False
        self.active = False

    # -- leadership (the candle warm-up pattern) ---------------------------------------------------
    def _is_leader(self) -> bool:
        if not constants.ENABLE_LEADER_ELECTION:
            return True
        if not self.leader_election:
            return False  # fail-closed: election enabled but unavailable
        return bool(self.leader_election.is_leader)

    def _leader_poll_seconds(self) -> float:
        interval = getattr(self.leader_election, "heartbeat_interval", None)
        if isinstance(interval, int | float) and interval > 0:
            return float(interval)
        return LEADER_POLL_FALLBACK_SECONDS

    def _report_active(self, active: bool) -> None:
        if self.on_active is not None:
            try:
                self.on_active(active)
            except Exception as exc:  # a gauge callback must never stop the loop
                logger.error("strategy_stats_refresh: on_active failed: %s", exc)

    def _set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        logger.info(
            "strategy_stats_refresh: %s",
            "leader; refreshing"
            if active
            else "not the leader; waiting for leadership",
        )
        self._report_active(active)

    async def _sleep_while_leader(self, seconds: float, poll: float) -> bool:
        remaining = seconds
        while self.running and remaining > 0:
            chunk = min(remaining, poll)
            try:
                await asyncio.sleep(chunk)
            except asyncio.CancelledError:
                self._set_active(False)
                raise
            remaining -= chunk
            if not self._is_leader():
                self._set_active(False)
                return self.running
        return self.running

    # -- one run -----------------------------------------------------------------------------------
    async def run_once(self) -> dict[str, Any]:
        """Compute, transform and store one snapshot; returns it."""
        from data_manager.services.exchange_ground_truth_inputs import (
            compute_ground_truth,
        )

        compute = self._compute or compute_ground_truth
        store = self._store or StrategyStatsSnapshotStore(
            self.db_manager.mongodb_adapter
        )
        started = time.monotonic()
        async with asyncio.timeout(RUN_BUDGET_SECONDS):
            ground_truth = await compute(self.db_manager, WINDOW_DAYS)
        strategies = build_strategy_stats(ground_truth, window_days=WINDOW_DAYS)
        duration = time.monotonic() - started
        metadata = ground_truth.get("metadata") or {}
        snapshot = {
            "computed_at": datetime.now(UTC),
            "window_days": WINDOW_DAYS,
            "window": metadata.get("window"),
            "rule": RULE,
            "strategies": strategies,
            "reconciliation": ground_truth.get("reconciliation"),
            "unmatched_headline": ground_truth.get("unmatched_headline"),
            "source": metadata.get("source"),
            "duration_seconds": duration,
        }
        await store.save(snapshot)
        strategy_stats_refresh_seconds.set(duration)
        strategy_stats_refresh_last_success.set(time.time())
        logger.info(
            "strategy_stats_refresh: stored %d strategies in %.1f s (episodes=%s, gap=%s)",
            len(strategies),
            duration,
            (ground_truth.get("unmatched_headline") or {}).get("episodes"),
            (ground_truth.get("reconciliation") or {}).get("gap"),
        )
        return snapshot

    # -- the loop ----------------------------------------------------------------------------------
    async def start(self) -> None:
        if not _enabled():
            logger.info(
                "strategy_stats_refresh: disabled (ENABLE_STRATEGY_STATS_REFRESH=false)"
            )
            return
        poll = self._leader_poll_seconds()
        self.running = True
        self._report_active(False)  # a follower reads 0, not "no series"
        try:
            await asyncio.sleep(INITIAL_DELAY_SECONDS)
            while self.running:
                if not self._is_leader():
                    self._set_active(False)
                    await asyncio.sleep(poll)
                    continue
                self._set_active(True)
                try:
                    await self.run_once()
                    strategy_stats_refresh_total.labels(result="success").inc()
                    sleep_for = REFRESH_INTERVAL_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    strategy_stats_refresh_total.labels(result="failure").inc()
                    logger.error(
                        "strategy_stats_refresh: run failed: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    sleep_for = ERROR_BACKOFF_SECONDS
                if not await self._sleep_while_leader(sleep_for, poll):
                    break
        finally:
            self.running = False
            self._set_active(False)
            logger.info("strategy_stats_refresh: stopped")

    async def stop(self) -> None:
        self.running = False
