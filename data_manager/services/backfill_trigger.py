"""
Backfill trigger — bridges analytics health verdicts to the backfill orchestrator.

The analytics scheduler (data_manager/analytics/scheduler.py) computes metrics
independently of data readiness.  When the gap detector evaluator (auditor/evaluator.py)
publishes an ``unhealthy`` verdict on ``evaluator.data-manager.verdict``, this module
consumes that verdict and triggers a backfill request for the affected symbols and
timeframes.

This satisfies AC4 of petrosa-data-manager#317: *connect analytics calculators to
trigger backfill on insufficient data*.

Usage
-----
The trigger is wired into ``main.py`` at startup alongside the other subsystem
evaluators.  No manual intervention is required.

    from data_manager.services.backfill_trigger import BackfillTrigger
    trigger = BackfillTrigger(db_manager, backfill_orchestrator)
    await trigger.start()
    # ... later ...
    await trigger.on_verdict("unhealthy", "12 gap(s) detected in cycle (worst: BTCUSDT 1m, 3600s)")
    await trigger.stop()
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Histogram

import constants
from data_manager.auditor.gap_detector import GapDetector
from data_manager.db.database_manager import DatabaseManager
from data_manager.models.events import BackfillRequest
from data_manager.services.backfill_queue import BackfillRequestQueue
from data_manager.utils.time_utils import parse_timeframe_to_seconds

logger = logging.getLogger(__name__)

# Prometheus metrics for the backfill trigger bridge
triggered_backfills_total = Counter(
    "data_manager_trigger_backfills_total",
    "Backfill jobs triggered by the analytics-health bridge",
    ["trigger_source", "symbol", "timeframe"],
)
backfill_request_latency = Histogram(
    "data_manager_backfill_request_latency_seconds",
    "Time from verdict received to backfill request submitted",
    ["trigger_source"],
)

# Minimum data age (seconds) below which we consider the data "fresh enough"
# and skip backfill.  Prevents triggering backfill on transient dips that
# resolve before the next audit cycle.
MIN_DATA_AGE_SECONDS = 300  # 5 minutes

# Maximum backfill window per trigger (seconds).  Prevents requesting months
# of backfill in one shot when multiple symbols/timeframes are affected.
MAX_BACKFILL_WINDOW_SECONDS = 86400  # 24 hours


class BackfillTrigger:
    """Consumes evaluator verdicts and triggers backfill when data health is poor."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        backfill_orchestrator=None,
        backfill_queue: BackfillRequestQueue | None = None,
    ):
        self.db_manager = db_manager
        self.backfill_orchestrator = backfill_orchestrator
        self.backfill_queue = backfill_queue
        self.gap_detector = GapDetector(
            db_manager,
            backfill_orchestrator=backfill_orchestrator,
            backfill_queue=backfill_queue,
        )
        self.running = False
        # Track recent verdicts to avoid duplicate triggers
        self._recent_verdicts: dict[str, datetime] = {}
        self._verdict_cooldown_seconds = 300  # 5 min per verdict key

    async def start(self) -> None:
        """Start the backfill trigger."""
        self.running = True
        logger.info("Backfill trigger started")

    async def stop(self) -> None:
        """Stop the backfill trigger."""
        self.running = False
        logger.info("Backfill trigger stopped")

    def _verdict_key(self, verdict: str, reason: str) -> str:
        """Create a dedup key from verdict + reason (first 100 chars)."""
        return f"{verdict}:{reason[:100]}"

    async def on_verdict(
        self, verdict: str, reason: str, auditor_only: bool = True
    ) -> list[BackfillRequest]:
        """
        Handle an incoming evaluator verdict.

        When the verdict is ``unhealthy``, this method:
        1. Checks the cooldown to avoid duplicate triggers.
        2. If the auditor flag is set, queries the gap detector for the
           current worst gap and triggers a backfill for it.
        3. If auditor_only is False (i.e. called from the analytics path),
           checks candle readiness and triggers backfill for any not-ready
           collections.

        Returns:
            List of BackfillRequest objects that were triggered.
        """
        if not self.running:
            return []

        if not self.backfill_orchestrator:
            # No orchestrator — queue if we have one, otherwise skip
            if self.backfill_queue:
                logger.warning(
                    "Backfill orchestrator unavailable; verdict-driven backfill "
                    "requests will be queued"
                )
            return []

        key = self._verdict_key(verdict, reason)
        last_time = self._recent_verdicts.get(key)
        if (
            last_time
            and (datetime.now(UTC) - last_time).total_seconds()
            < self._verdict_cooldown_seconds
        ):
            logger.debug(f"Verdict {key} within cooldown; skipping")
            return []

        if verdict != "unhealthy":
            return []

        start = datetime.now(UTC)
        requests: list[BackfillRequest] = []

        if auditor_only:
            # Audit path: the evaluator already knows the worst gap.
            # Parse the reason to extract the worst gap symbol/timeframe/duration.
            requests = await self._trigger_from_audit_reason(reason)
        else:
            # Analytics path: check candle readiness and trigger backfill
            # for collections that are not ready.
            requests = await self._trigger_from_readiness_check()

        if requests:
            self._recent_verdicts[key] = datetime.now(UTC)
            latency = (datetime.now(UTC) - start).total_seconds()
            backfill_request_latency.labels(trigger_source="analytics_bridge").observe(
                latency
            )

        return requests

    async def _trigger_from_audit_reason(self, reason: str) -> list[BackfillRequest]:
        """
        Parse the evaluator's ``unhealthy`` reason string and trigger backfill
        for the worst gap.

        Expected format (from GapDetectorEvaluator.REASON_TEMPLATE_UNHEALTHY):
        "{count} gap(s) detected in cycle (worst: {symbol} {timeframe}, {duration_s}s)"
        """
        requests: list[BackfillRequest] = []

        # Parse the worst gap from the reason string
        # Format: "12 gap(s) detected in cycle (worst: BTCUSDT 1m, 3600s)"
        import re

        match = re.search(r"worst:\s+(\w+)\s+(\d+m|\d+h|\d+d),\s+(\d+)s", reason)
        if not match:
            logger.warning(f"Cannot parse worst gap from reason: {reason}")
            return []

        symbol = match.group(1)
        timeframe = match.group(2)
        duration_s = int(match.group(3))

        # Determine the gap window
        gap_end = datetime.now(UTC)
        gap_start = gap_end - timedelta(
            seconds=min(duration_s, MAX_BACKFILL_WINDOW_SECONDS)
        )

        # Only trigger if the gap exceeds the minimum threshold
        if duration_s < MIN_DATA_AGE_SECONDS:
            logger.debug(
                f"Gap {duration_s}s < MIN_DATA_AGE {MIN_DATA_AGE_SECONDS}s; "
                f"skipping backfill for {symbol} {timeframe}"
            )
            return []

        request = BackfillRequest(
            symbol=symbol,
            data_type="candles",
            timeframe=timeframe,
            start_time=gap_start,
            end_time=gap_end,
            priority=1,
            source="analytics_bridge",
        )

        try:
            job = await self.backfill_orchestrator.create_backfill_job(request)
            triggered_backfills_total.labels(
                trigger_source="analytics_bridge",
                symbol=symbol,
                timeframe=timeframe,
            ).inc()
            logger.info(
                f"Triggered backfill via analytics bridge: "
                f"{job.job_id} for {symbol} {timeframe} "
                f"({gap_start.isoformat()} to {gap_end.isoformat()})"
            )
            requests.append(request)
        except Exception as e:
            logger.warning(
                f"Orchestrator call failed for {symbol} {timeframe}: {e}. "
                "Queuing request for later delivery."
            )
            # Fallback: queue the request
            if self.backfill_queue:
                await self.backfill_queue.add(request)
                logger.info(
                    f"Queued backfill for {symbol} {timeframe} "
                    f"(queue size: {self.backfill_queue.size})"
                )
                requests.append(request)
            else:
                logger.error(
                    f"Failed to trigger backfill for {symbol} {timeframe}: {e}",
                    exc_info=True,
                )

        return requests

    async def _trigger_from_readiness_check(self) -> list[BackfillRequest]:
        """
        Check candle readiness and trigger backfill for not-ready collections.

        This is called from the analytics scheduler when it detects that
        analytics data is insufficient due to missing candles.
        """
        from data_manager.db.mongodb_adapter import MongoDBAdapter
        from data_manager.maintenance.candle_readiness import evaluate_readiness

        requests: list[BackfillRequest] = []

        try:
            connection_string = constants.MONGODB_URL
            if not connection_string:
                logger.warning("MONGODB_URL not set; skipping readiness check")
                return []

            adapter = MongoDBAdapter(connection_string=connection_string)
            adapter.connect()
            try:
                report = await evaluate_readiness(
                    adapter,
                    pairs=list(constants.SUPPORTED_PAIRS),
                    timeframes=list(constants.SUPPORTED_INTERVALS),
                )

                for coll in report.not_ready:
                    # Determine the backfill window: the collection is not ready
                    # because it's either too shallow or too stale.
                    # For depth issues, backfill from the oldest known candle.
                    # For freshness issues, backfill the last 24 hours.
                    if any("depth" in r for r in coll.reasons):
                        # Depth issue: try to fill from 24h ago
                        gap_end = datetime.now(UTC)
                        gap_start = gap_end - timedelta(
                            seconds=MAX_BACKFILL_WINDOW_SECONDS
                        )
                    elif any("newest" in r for r in coll.reasons):
                        # Freshness issue: fill the gap from the last ready point
                        gap_end = datetime.now(UTC)
                        gap_start = gap_end - timedelta(
                            seconds=MAX_BACKFILL_WINDOW_SECONDS
                        )
                    else:
                        # Unknown reason: skip
                        logger.warning(
                            f"Unknown readiness failure for {coll.collection}: {coll.reasons}"
                        )
                        continue

                    request = BackfillRequest(
                        symbol=coll.pair,
                        data_type="candles",
                        timeframe=coll.timeframe,
                        start_time=gap_start,
                        end_time=gap_end,
                        priority=2,
                        source="analytics_bridge_readiness",
                    )

                    try:
                        job = await self.backfill_orchestrator.create_backfill_job(
                            request
                        )
                        triggered_backfills_total.labels(
                            trigger_source="analytics_bridge_readiness",
                            symbol=coll.pair,
                            timeframe=coll.timeframe,
                        ).inc()
                        logger.info(
                            f"Triggered readiness backfill: "
                            f"{job.job_id} for {coll.pair} {coll.timeframe}"
                        )
                        requests.append(request)
                    except Exception as e:
                        logger.warning(
                            f"Orchestrator call failed for {coll.pair} {coll.timeframe}: {e}. "
                            "Queuing request for later delivery."
                        )
                        # Fallback: queue the request
                        if self.backfill_queue:
                            await self.backfill_queue.add(request)
                            requests.append(request)
                        else:
                            logger.error(
                                f"Failed to trigger backfill for {coll.pair} {coll.timeframe}: {e}",
                                exc_info=True,
                            )
            finally:
                adapter.disconnect()

        except Exception as e:
            logger.error(f"Readiness check failed: {e}", exc_info=True)

        return requests
