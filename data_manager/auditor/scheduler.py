"""
Audit scheduler for periodic data quality checks.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from prometheus_client import Counter, Gauge, Histogram

import constants
from data_manager.auditor.duplicate_detector import DuplicateDetector
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
    ):
        """
        Initialize audit scheduler.

        Args:
            db_manager: Database manager instance
            leader_election: Leader election manager (optional)
            backfill_orchestrator: Backfill orchestrator for auto-backfill (optional)
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
        logger.info("Audit scheduler started")

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
        audit_start = datetime.utcnow()

        # Define audit window (last 24 hours)
        end = datetime.utcnow()
        start = end - timedelta(hours=24)

        symbols_audited = 0
        total_gaps = 0
        total_duplicates = 0

        # Audit each supported symbol and timeframe
        for symbol in constants.SUPPORTED_PAIRS:
            for timeframe in constants.SUPPORTED_TIMEFRAMES:
                try:
                    # Detect gaps
                    gaps = await self.gap_detector.detect_gaps(
                        symbol, timeframe, start, end
                    )
                    total_gaps += len(gaps)

                    if gaps:
                        logger.warning(
                            f"Found {len(gaps)} gaps for {symbol} {timeframe}"
                        )
                        # Update metrics
                        audit_gaps_detected.labels(
                            symbol=symbol, timeframe=timeframe
                        ).inc(len(gaps))

                    # Detect duplicates
                    duplicates = await self.duplicate_detector.detect_duplicates(
                        symbol, timeframe, start, end
                    )
                    total_duplicates += duplicates

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
                        f"gaps={len(gaps)}, "
                        f"duplicates={duplicates}"
                    )

                    symbols_audited += 1

                except Exception as e:
                    logger.warning(f"Error auditing {symbol} {timeframe}: {e}")

        audit_duration = (datetime.utcnow() - audit_start).total_seconds()

        # Update cycle duration metric
        audit_cycle_duration.observe(audit_duration)

        # Store last audit time
        self.last_audit_time = datetime.utcnow()

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
