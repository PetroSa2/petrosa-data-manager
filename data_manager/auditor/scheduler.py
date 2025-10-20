"""
Audit scheduler for periodic data quality checks.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import constants
from data_manager.auditor.gap_detector import GapDetector
from data_manager.auditor.health_scorer import HealthScorer
from data_manager.db.database_manager import DatabaseManager

logger = logging.getLogger(__name__)


class AuditScheduler:
    """
    Schedules and orchestrates periodic data audits.

    Runs gap detection and health scoring for all symbols and timeframes.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize audit scheduler.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.gap_detector = GapDetector(db_manager)
        self.health_scorer = HealthScorer(db_manager)
        self.running = False

    async def start(self) -> None:
        """Start the audit scheduler."""
        self.running = True
        logger.info("Audit scheduler started")

        while self.running:
            try:
                await self.run_audit_cycle()
                await asyncio.sleep(constants.AUDIT_INTERVAL)
            except Exception as e:
                logger.error(f"Error in audit scheduler: {e}", exc_info=True)
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

                    # Calculate health metrics
                    health = await self.health_scorer.calculate_health(
                        symbol, timeframe, lookback_hours=24
                    )

                    logger.debug(
                        f"Health for {symbol} {timeframe}: "
                        f"completeness={health.completeness:.1f}%, "
                        f"quality={health.quality_score:.1f}"
                    )

                    symbols_audited += 1

                except Exception as e:
                    logger.error(
                        f"Error auditing {symbol} {timeframe}: {e}",
                        exc_info=True,
                    )

        audit_duration = (datetime.utcnow() - audit_start).total_seconds()

        logger.info(
            f"Audit cycle complete: "
            f"audited={symbols_audited}, "
            f"gaps={total_gaps}, "
            f"duration={audit_duration:.1f}s"
        )

