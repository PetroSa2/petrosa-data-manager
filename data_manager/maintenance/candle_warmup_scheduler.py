"""
Continuous candle warm-up backfill scheduler (petrosa-data-manager#319).

#275 shipped :mod:`data_manager.maintenance.candle_warmup_backfill` as a
**one-shot cutover tool**: run it once, prove the grid warm via the #275 AC2
readiness gate, then flip MongoDB to primary (#274). Nothing re-ran it. Gaps
that formed *after* the flip — a missed extractor window, an Atlas blip, a new
pair added to ``SUPPORTED_PAIRS`` — were never refilled, so
``candles_{pair}_{timeframe}`` collections drift shallow and stale until a
human notices.

This module closes that loop. It does **not** reimplement backfilling: it wraps
the existing :func:`~data_manager.maintenance.candle_warmup_backfill.run_backfill`
in a periodic, leader-elected loop that lives in the long-running data-manager
process.

Design — continuous in-process loop, not a Kubernetes CronJob
-------------------------------------------------------------
Both were considered (#319 AC1). The in-process loop won on three counts:

1. **Metrics actually arrive.** Short-lived CronJob pods push OTLP and
   routinely die before the final export flushes; the data-manager Deployment
   is already a live Prometheus scrape target, so
   ``data_manager_candle_backfill_collections_total`` is observable the moment
   it moves — which is what AC4/AC5 need to alert on.
2. **Single writer for free.** :class:`LeaderElectionManager` already
   guarantees exactly one active writer across replicas. A CronJob overlapping
   a slow run would need its own concurrency policy and would double-write.
3. **Credentials and adapters already exist** in-process; a CronJob needs its
   own manifest, secret wiring and image-tag lifecycle in a *different* repo.

The CLI entrypoint is untouched, so the CronJob route stays available for
operators who want an out-of-band sweep
(``python -m data_manager.maintenance.candle_warmup_backfill``); see
``docs/candle-warmup-continuous-backfill.md``.

Safety
------
- **Off by default** (``ENABLE_CANDLE_WARMUP_SCHEDULER=false``). No new
  MongoDB writer turns itself on — the standing rule after four Atlas M0 quota
  P0s (k8s#783/#819/#881/#899).
- **Bounded writes.** Every cycle delegates to the #275 backfill, which trims
  each touched collection back to
  ``CANDLE_WARMUP_MIN_CANDLES * CANDLE_WARMUP_TRIM_FACTOR``.
- **Cheap when healthy.** The readiness gate short-circuits each already-warm
  collection (AC3), so a healthy grid performs count/latest reads and zero
  writes.
- **Never fatal.** A failing cycle is counted, logged and retried after a
  backoff; it can neither kill the loop nor the process.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram

import constants
from data_manager.maintenance.candle_warmup_backfill import (
    BackfillResult,
    load_config_from_env,
    run_backfill,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CandleWarmupScheduler",
    "candle_backfill_collections_total",
]

# --- Metrics -------------------------------------------------------------
# Flat `data_manager_*` names, matching the existing candle counters from
# PR #280 (`data_manager_candle_read_fallbacks_total`, ...). The AC4 counter
# is labelled by outcome so a single series answers both "is it doing work?"
# and "is it failing?".
candle_backfill_collections_total = Counter(
    "data_manager_candle_backfill_collections_total",
    "Candle collections processed by the continuous warm-up backfill",
    ["timeframe", "outcome"],
)

candle_backfill_candles_written_total = Counter(
    "data_manager_candle_backfill_candles_written_total",
    "Candles written into Mongo candles_* by the continuous warm-up backfill",
    ["timeframe"],
)

candle_backfill_cycle_seconds = Histogram(
    "data_manager_candle_backfill_cycle_seconds",
    "Duration of one continuous warm-up backfill cycle in seconds",
)

candle_backfill_cycles_total = Counter(
    "data_manager_candle_backfill_cycles_total",
    "Continuous warm-up backfill cycles completed",
    ["outcome"],
)

# Epoch seconds of the last cycle that completed without a per-collection
# error. This is the "falling behind" signal: `time() - <gauge>` is the age of
# the last known-good sweep. 0 means "never succeeded since process start".
candle_backfill_last_success_timestamp = Gauge(
    "data_manager_candle_backfill_last_success_timestamp",
    "Unix timestamp of the last fully successful warm-up backfill cycle",
)

candle_backfill_active = Gauge(
    "data_manager_candle_backfill_active",
    "Continuous warm-up backfill scheduler active on this pod (1=active)",
)

# How many collections the most recent cycle found not-ready and had to warm.
# Steady state is 0; a persistently non-zero value means the upstream write
# path is losing candles faster than the sweep restores them.
candle_backfill_collections_needing_warmup = Gauge(
    "data_manager_candle_backfill_collections_needing_warmup",
    "Collections that failed the readiness gate during the last cycle",
)


def _outcome(result: BackfillResult) -> str:
    """Classify one ``BackfillResult`` into a metric outcome label."""
    if result.error:
        return "failed"
    if result.skipped:
        return "skipped"
    if result.written:
        return "written"
    # Reached the backfill but had nothing to write — e.g. the source table
    # held no rows for the pair, or every row was unmappable. Distinct from
    # "skipped" (already warm) because it is NOT a healthy steady state.
    return "no_source_data"


class CandleWarmupScheduler:
    """Periodically re-run the #275 warm-up backfill to maintain candle depth.

    Args:
        db_manager: Provides the live ``mysql_adapter`` (source) and
            ``mongodb_adapter`` (destination). The scheduler borrows both; it
            never connects or disconnects them, because they are owned by the
            application lifecycle.
        leader_election: When leader election is enabled, only the leader pod
            runs cycles, so N replicas still yield exactly one writer.
    """

    def __init__(self, db_manager, leader_election=None) -> None:
        self.db_manager = db_manager
        self.leader_election = leader_election
        self.running = False
        self.last_cycle_time: datetime | None = None
        self.last_success_time: datetime | None = None
        self.cycles_run = 0
        self.consecutive_failures = 0

    # --- lifecycle -------------------------------------------------------

    def _is_leader(self) -> bool:
        """Return True when this pod may write.

        Fail-closed when leader election is enabled but unavailable: a
        scheduler that cannot prove it is the single writer must not write.
        """
        if not constants.ENABLE_LEADER_ELECTION:
            return True
        if not self.leader_election:
            logger.error(
                "candle_warmup_scheduler: leader election enabled but no "
                "LeaderElectionManager provided; refusing to run"
            )
            return False
        return bool(self.leader_election.is_leader)

    async def start(self) -> None:
        """Run warm-up cycles until :meth:`stop` is called."""
        if not constants.ENABLE_CANDLE_WARMUP_SCHEDULER:
            logger.info(
                "candle_warmup_scheduler: disabled "
                "(ENABLE_CANDLE_WARMUP_SCHEDULER=false)"
            )
            return

        if not self._is_leader():
            logger.info(
                "candle_warmup_scheduler: not the leader; scheduler idle on this pod"
            )
            candle_backfill_active.set(0)
            return

        interval = max(60, int(constants.CANDLE_WARMUP_SCHEDULER_INTERVAL))
        backoff = max(10, int(constants.CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF))
        initial_delay = max(0, int(constants.CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY))

        self.running = True
        candle_backfill_active.set(1)
        logger.info(
            "candle_warmup_scheduler: starting (interval=%ds, initial_delay=%ds)",
            interval,
            initial_delay,
        )

        try:
            await asyncio.sleep(initial_delay)
        except asyncio.CancelledError:
            self.running = False
            candle_backfill_active.set(0)
            return

        while self.running:
            if not self._is_leader():
                logger.warning(
                    "candle_warmup_scheduler: lost leadership; stopping on this pod"
                )
                break

            try:
                await self.run_cycle()
                sleep_for = interval
            except Exception as exc:
                # run_cycle is already failure-isolated per collection; this
                # catches the outer failures (adapters gone, config error) so
                # the loop itself can never die.
                self.consecutive_failures += 1
                candle_backfill_cycles_total.labels(outcome="failed").inc()
                logger.error(
                    "candle_warmup_scheduler: cycle failed (%d consecutive): %s",
                    self.consecutive_failures,
                    exc,
                )
                sleep_for = backoff

            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

        self.running = False
        candle_backfill_active.set(0)
        logger.info("candle_warmup_scheduler: stopped")

    async def stop(self) -> None:
        """Signal the loop to exit after the in-flight sleep/cycle."""
        self.running = False
        candle_backfill_active.set(0)

    # --- one cycle -------------------------------------------------------

    async def run_cycle(self) -> list[BackfillResult]:
        """Run exactly one warm-up sweep across the configured grid.

        Returns the per-collection results so callers (and tests) can assert
        on them. Raises only when the adapters are missing — every
        per-collection failure is captured in ``BackfillResult.error`` by the
        #275 job itself.
        """
        mysql = getattr(self.db_manager, "mysql_adapter", None)
        mongo = getattr(self.db_manager, "mongodb_adapter", None)
        if mysql is None or mongo is None:
            raise RuntimeError(
                "candle_warmup_scheduler: db_manager is missing mysql_adapter "
                "or mongodb_adapter"
            )

        config = load_config_from_env()
        started = datetime.now(UTC)

        results = await run_backfill(mysql, mongo, config)

        duration = (datetime.now(UTC) - started).total_seconds()
        candle_backfill_cycle_seconds.observe(duration)
        self.last_cycle_time = datetime.now(UTC)
        self.cycles_run += 1

        self._record(results)

        failed = [r for r in results if r.error]
        needing = [r for r in results if not r.skipped and not r.error]
        candle_backfill_collections_needing_warmup.set(len(needing))

        if failed:
            self.consecutive_failures += 1
            candle_backfill_cycles_total.labels(outcome="failed").inc()
            logger.warning(
                "candle_warmup_scheduler: cycle finished with %d/%d failed "
                "collections in %.1fs: %s",
                len(failed),
                len(results),
                duration,
                ", ".join(r.collection for r in failed[:10]),
            )
        else:
            self.consecutive_failures = 0
            self.last_success_time = self.last_cycle_time
            candle_backfill_last_success_timestamp.set(self.last_cycle_time.timestamp())
            candle_backfill_cycles_total.labels(outcome="success").inc()
            logger.info(
                "candle_warmup_scheduler: cycle complete — %d collections, "
                "%d warmed, %d already warm, %d candles written, %.1fs",
                len(results),
                len(needing),
                len(results) - len(needing),
                sum(r.written for r in results),
                duration,
            )

        return results

    @staticmethod
    def _record(results: list[BackfillResult]) -> None:
        """Fold one cycle's results into the Prometheus counters (AC4)."""
        for result in results:
            candle_backfill_collections_total.labels(
                timeframe=result.timeframe, outcome=_outcome(result)
            ).inc()
            if result.written:
                candle_backfill_candles_written_total.labels(
                    timeframe=result.timeframe
                ).inc(result.written)

    # --- introspection ---------------------------------------------------

    def get_status(self) -> dict:
        """Return scheduler state for the service status endpoint."""
        return {
            "enabled": bool(constants.ENABLE_CANDLE_WARMUP_SCHEDULER),
            "running": self.running,
            "interval_seconds": int(constants.CANDLE_WARMUP_SCHEDULER_INTERVAL),
            "cycles_run": self.cycles_run,
            "consecutive_failures": self.consecutive_failures,
            "last_cycle_time": (
                self.last_cycle_time.isoformat() if self.last_cycle_time else None
            ),
            "last_success_time": (
                self.last_success_time.isoformat() if self.last_success_time else None
            ),
        }
