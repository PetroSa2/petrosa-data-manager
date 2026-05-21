"""
Audit scheduler for periodic data quality checks.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from petrosa_otel.evaluators import NatsVerdictPublisher
from prometheus_client import Counter, Gauge, Histogram

import constants
from data_manager.auditor.duplicate_detector import DuplicateDetector
from data_manager.auditor.evaluator import GapDetectorEvaluator
from data_manager.auditor.gap_detector import GapDetector
from data_manager.auditor.health_scorer import HealthScorer
from data_manager.db.database_manager import DatabaseManager
from data_manager.leader_election import LeaderElectionManager

logger = logging.getLogger(__name__)

# Prometheus metrics
audit_cycle_duration = Histogram(
    "data_manager_audit_cycle_seconds",
    "Audit cycle duration in seconds",
)
audit_gaps_detected = Counter(
    "data_manager_audit_gaps_detected_total",
    "Total gaps detected",
    ["symbol", "timeframe"],
)
audit_duplicates_detected = Counter(
    "data_manager_audit_duplicates_detected_total",
    "Total duplicates detected",
    ["symbol", "timeframe"],
)
audit_health_score = Gauge(
    "data_manager_audit_health_score",
    "Dataset health score (0-100)",
    ["symbol", "timeframe"],
)
audit_leader_status = Gauge(
    "data_manager_audit_leader_status",
    "Leader election status (1=leader, 0=follower)",
)
audit_backfills_triggered = Counter(
    "data_manager_audit_backfills_triggered_total",
    "Auto-triggered backfill jobs",
    ["symbol", "timeframe"],
)


class AuditScheduler:
    """
    Schedules and orchestrates periodic data audits.

    Runs gap detection and health scoring for all symbols and timeframes.
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        leader_election: LeaderElectionManager | None = None,
        backfill_orchestrator=None,
        nats_client=None,
    ):
        """
        Initialize audit scheduler.

        Args:
            db_manager: Database manager instance
            leader_election: Leader election manager (optional)
            backfill_orchestrator: Backfill orchestrator for auto-backfill (optional)
            nats_client: Underlying nats.aio.client.Client used by the
                P2.1 evaluator framework to publish health verdicts on
                ``evaluator.data-manager.verdict``. When ``None`` the
                evaluator is still instantiated but no verdicts are
                published — useful for tests and local runs without a
                broker.
        """
        self.db_manager = db_manager
        self.gap_detector = GapDetector(
            db_manager, backfill_orchestrator=backfill_orchestrator
        )
        self.duplicate_detector = DuplicateDetector(db_manager)
        self.health_scorer = HealthScorer(db_manager)
        self.leader_election = leader_election
        self.backfill_orchestrator = backfill_orchestrator
        self.running = False
        self.last_audit_time: datetime | None = None

        # P2.1 consumer (#634): wire the audit cycle into the shared
        # subsystem-evaluator framework. The publisher is optional so the
        # scheduler is still usable without a live NATS connection.
        publisher = (
            NatsVerdictPublisher(nats_client=nats_client)
            if nats_client is not None
            else None
        )
        self.evaluator: GapDetectorEvaluator = GapDetectorEvaluator(publisher=publisher)

    async def start(self) -> None:
        """Start the audit scheduler."""
        # Check if leader election is enabled and required
        if constants.ENABLE_LEADER_ELECTION:
            if not self.leader_election:
                logger.error(
                    "Leader election is enabled but no LeaderElectionManager provided"
                )
                return

            # Check if this pod is the leader
            if not self.leader_election.is_leader:
                logger.info(
                    f"Pod {self.leader_election.pod_id} is not the leader. "
                    f"Audit scheduler will not run on this pod."
                )
                audit_leader_status.set(0)
                return

            logger.info(
                f"Pod {self.leader_election.pod_id} is the LEADER. "
                f"Starting audit scheduler."
            )
            audit_leader_status.set(1)
        else:
            logger.warning(
                "Leader election is DISABLED. This may cause duplicate work across replicas!"
            )
            audit_leader_status.set(1)  # Assume leader if election disabled

        self.running = True
        logger.info(
            f"Audit scheduler starting (delaying {constants.INITIAL_STARTUP_DELAY}s)"
        )

        # Give the service time to stabilize and pass health checks before starting cycles
        await asyncio.sleep(constants.INITIAL_STARTUP_DELAY)

        logger.info("Audit scheduler active")

        while self.running:
            try:
                # Verify leadership if leader election is enabled
                if (
                    constants.ENABLE_LEADER_ELECTION
                    and self.leader_election
                    and not self.leader_election.is_leader
                ):
                    logger.warning(
                        "Lost leadership! Stopping audit scheduler on this pod."
                    )
                    audit_leader_status.set(0)
                    break

                await self.run_audit_cycle()
                await asyncio.sleep(constants.AUDIT_INTERVAL)
            except Exception as e:
                logger.warning(
                    f"Audit cycle failed: {e}. Will retry in {constants.AUDIT_INTERVAL}s"
                )
                await asyncio.sleep(30)  # Short backoff on error

        logger.info("Audit scheduler stopped")

    async def stop(self) -> None:
        """Stop the audit scheduler."""
        self.running = False

    async def run_audit_cycle(self) -> None:
        """Run a single audit cycle for all symbols and timeframes."""
        logger.info("Starting audit cycle")
        audit_start = datetime.now(UTC)

        # Define audit window (last 24 hours)
        end = datetime.now(UTC)
        start = end - timedelta(hours=24)

        symbols_audited = 0
        total_gaps = 0
        total_duplicates = 0
        worst_gap_summary: str | None = None
        worst_gap_duration = -1

        semaphore = asyncio.Semaphore(constants.MAX_CONCURRENT_TASKS)

        async def audit_subset(symbol, timeframe):
            async with semaphore:
                try:
                    # Detect gaps
                    gaps = await self.gap_detector.detect_gaps(
                        symbol, timeframe, start, end
                    )
                    gaps_count = len(gaps)
                    # Per #634: surface the worst (longest) gap so the
                    # cycle-level evaluator verdict carries the actionable
                    # summary, not just a count.
                    subset_worst_summary: str | None = None
                    subset_worst_duration = -1
                    for gap in gaps:
                        # Production callers return GapInfo objects; some
                        # unit tests pass plain dict stubs. getattr keeps
                        # this loop duck-typed without coupling to either
                        # representation.
                        dur = getattr(gap, "duration_seconds", 0) or 0
                        if dur > subset_worst_duration:
                            subset_worst_duration = dur
                            subset_worst_summary = f"{symbol} {timeframe}, {dur}s"

                    if gaps:
                        logger.warning(
                            f"Found {gaps_count} gaps for {symbol} {timeframe}"
                        )
                        # Update metrics
                        audit_gaps_detected.labels(
                            symbol=symbol, timeframe=timeframe
                        ).inc(gaps_count)

                    # Detect duplicates
                    duplicates = await self.duplicate_detector.detect_duplicates(
                        symbol, timeframe, start, end
                    )

                    if duplicates > 0:
                        logger.warning(
                            f"Found {duplicates} duplicates for {symbol} {timeframe}"
                        )
                        # Update metrics
                        audit_duplicates_detected.labels(
                            symbol=symbol, timeframe=timeframe
                        ).inc(duplicates)

                    # Calculate health metrics (now includes gaps and duplicates)
                    health = await self.health_scorer.calculate_health(
                        symbol,
                        timeframe,
                        lookback_hours=24,
                        gaps=gaps,
                        duplicates_count=duplicates,
                    )

                    # Update health score metric
                    audit_health_score.labels(symbol=symbol, timeframe=timeframe).set(
                        health.quality_score
                    )

                    logger.debug(
                        f"Health for {symbol} {timeframe}: "
                        f"completeness={health.completeness:.1f}%, "
                        f"quality={health.quality_score:.1f}, "
                        f"gaps={gaps_count}, "
                        f"duplicates={duplicates}"
                    )

                    return (
                        1,
                        gaps_count,
                        duplicates,
                        subset_worst_duration,
                        subset_worst_summary,
                    )

                except Exception as e:
                    logger.warning(f"Error auditing {symbol} {timeframe}: {e}")
                    return 0, 0, 0, -1, None

        # Audit each supported symbol and timeframe in parallel
        tasks = []
        for symbol in constants.SUPPORTED_PAIRS:
            for timeframe in constants.SUPPORTED_TIMEFRAMES:
                tasks.append(audit_subset(symbol, timeframe))

        results = await asyncio.gather(*tasks)

        # Sum up results
        for audited, gaps, duplicates, subset_dur, subset_summary in results:
            symbols_audited += audited
            total_gaps += gaps
            total_duplicates += duplicates
            if subset_dur > worst_gap_duration and subset_summary is not None:
                worst_gap_duration = subset_dur
                worst_gap_summary = subset_summary

        # Per #634: feed cycle aggregate into the P2.1 evaluator framework.
        # Hysteresis smooths transient blips; the publisher (if wired)
        # writes the post-hysteresis verdict to
        # ``evaluator.data-manager.verdict``.
        sample_verdict, sample_reason = GapDetectorEvaluator.cycle_sample(
            total_gaps=total_gaps,
            worst_gap_summary=worst_gap_summary,
        )
        try:
            await self.evaluator.tick_with_sample(sample_verdict, sample_reason)
        except Exception as exc:
            # The evaluator framework already swallows publish failures
            # internally; this guards against e.g. a hysteresis bug — we
            # never want an evaluator hiccup to break the audit cycle.
            logger.warning(f"Evaluator tick failed: {exc}")

        audit_duration = (datetime.now(UTC) - audit_start).total_seconds()

        # Update cycle duration metric
        audit_cycle_duration.observe(audit_duration)

        # Store last audit time
        self.last_audit_time = datetime.now(UTC)

        logger.info(
            f"Audit cycle complete: "
            f"audited={symbols_audited}, "
            f"gaps={total_gaps}, "
            f"duplicates={total_duplicates}, "
            f"duration={audit_duration:.1f}s"
        )

    def get_status(self) -> dict:
        """
        Get current audit scheduler status.

        Returns:
            Dictionary with status information
        """
        status = {
            "running": self.running,
            "last_audit_time": self.last_audit_time.isoformat()
            if self.last_audit_time
            else None,
        }

        if self.leader_election:
            status.update(self.leader_election.get_status())

        return status
