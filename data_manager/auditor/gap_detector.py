"""
Gap detection for time series data.
"""

import logging
from datetime import datetime, timedelta

from prometheus_client import Counter

import constants
from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import AuditRepository, CandleRepository
from data_manager.models.events import BackfillRequest
from data_manager.models.health import GapInfo
from data_manager.utils.time_utils import parse_timeframe_to_seconds

logger = logging.getLogger(__name__)

# Metrics
auto_backfill_triggered_counter = Counter(
    "data_manager_auto_backfill_triggered_total",
    "Total auto-triggered backfill jobs",
    ["symbol", "timeframe", "reason"],
)


class GapDetector:
    """
    Detects gaps in time series data.

    Uses MongoDB queries to find missing data ranges.
    """

    def __init__(self, db_manager: DatabaseManager, backfill_orchestrator=None):
        """
        Initialize gap detector.

        Args:
            db_manager: Database manager instance
            backfill_orchestrator: Optional backfill orchestrator for auto-backfill
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.audit_repo = AuditRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.backfill_orchestrator = backfill_orchestrator

    async def detect_gaps(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[GapInfo]:
        """
        Detect gaps in candle data.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            start: Start datetime
            end: End datetime

        Returns:
            List of GapInfo objects
        """
        try:
            logger.debug(
                f"Detecting gaps for {symbol} {timeframe} from {start} to {end}"
            )

            # Get all candles in range
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if not candles:
                # Entire range is a gap
                logger.warning(f"No data found for {symbol} {timeframe}")
                gap = GapInfo(
                    start_time=start,
                    end_time=end,
                    duration_seconds=int((end - start).total_seconds()),
                    expected_records=self._calculate_expected_records(
                        start, end, timeframe
                    ),
                )
                await self._log_gap(symbol, timeframe, gap)
                return [gap]

            # Parse timestamps
            timestamps = [
                (
                    candle["timestamp"]
                    if isinstance(candle["timestamp"], datetime)
                    else datetime.fromisoformat(str(candle["timestamp"]))
                )
                for candle in candles
            ]
            timestamps.sort()

            # Calculate expected interval
            interval_seconds = parse_timeframe_to_seconds(timeframe)
            expected_interval = timedelta(seconds=interval_seconds)
            tolerance = timedelta(seconds=constants.GAP_TOLERANCE_SECONDS)

            # Find gaps
            gaps = []

            # Check for gap at the beginning
            if timestamps[0] > start + expected_interval + tolerance:
                gap = GapInfo(
                    start_time=start,
                    end_time=timestamps[0],
                    duration_seconds=int((timestamps[0] - start).total_seconds()),
                    expected_records=self._calculate_expected_records(
                        start, timestamps[0], timeframe
                    ),
                )
                gaps.append(gap)
                await self._log_gap(symbol, timeframe, gap)

            # Check for gaps between consecutive timestamps
            for i in range(len(timestamps) - 1):
                current_ts = timestamps[i]
                next_ts = timestamps[i + 1]
                expected_next = current_ts + expected_interval

                if next_ts > expected_next + tolerance:
                    gap = GapInfo(
                        start_time=expected_next,
                        end_time=next_ts,
                        duration_seconds=int((next_ts - expected_next).total_seconds()),
                        expected_records=self._calculate_expected_records(
                            expected_next, next_ts, timeframe
                        ),
                    )
                    gaps.append(gap)
                    await self._log_gap(symbol, timeframe, gap)

            # Check for gap at the end
            if timestamps[-1] < end - expected_interval - tolerance:
                gap = GapInfo(
                    start_time=timestamps[-1] + expected_interval,
                    end_time=end,
                    duration_seconds=int((end - timestamps[-1]).total_seconds()),
                    expected_records=self._calculate_expected_records(
                        timestamps[-1] + expected_interval, end, timeframe
                    ),
                )
                gaps.append(gap)
                await self._log_gap(symbol, timeframe, gap)

            if gaps:
                logger.debug(f"Found {len(gaps)} gaps for {symbol} {timeframe}")
            else:
                logger.debug(f"No gaps found for {symbol} {timeframe}")
            return gaps

        except Exception as e:
            logger.error(
                f"Error detecting gaps for {symbol} {timeframe}: {e}", exc_info=True
            )
            return []

    def _calculate_expected_records(
        self, start: datetime, end: datetime, timeframe: str
    ) -> int:
        """Calculate expected number of records."""
        interval_seconds = parse_timeframe_to_seconds(timeframe)
        duration_seconds = (end - start).total_seconds()
        return int(duration_seconds / interval_seconds)

    async def _log_gap(self, symbol: str, timeframe: str, gap: GapInfo) -> None:
        """Log detected gap to audit logs."""
        try:
            dataset_id = f"candles_{symbol}_{timeframe}"
            severity = "high" if gap.duration_seconds > 3600 else "medium"
            await self.audit_repo.log_gap(
                dataset_id=dataset_id,
                symbol=symbol,
                gap_start=gap.start_time,
                gap_end=gap.end_time,
                severity=severity,
            )

            # Trigger auto-backfill if enabled and gap meets threshold
            if constants.ENABLE_AUTO_BACKFILL:
                await self._trigger_backfill(symbol, timeframe, gap, severity)

        except Exception as e:
            logger.error(f"Failed to log gap: {e}")

    async def _trigger_backfill(
        self, symbol: str, timeframe: str, gap: GapInfo, severity: str
    ) -> None:
        """
        Trigger automatic backfill for detected gap.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe
            gap: Gap information
            severity: Gap severity level
        """
        try:
            # Check if gap exceeds minimum threshold
            if gap.duration_seconds < constants.MIN_AUTO_BACKFILL_GAP:
                logger.debug(
                    f"Gap too small for auto-backfill: {gap.duration_seconds}s "
                    f"(threshold: {constants.MIN_AUTO_BACKFILL_GAP}s)"
                )
                return

            # Check if backfill orchestrator is available
            if not self.backfill_orchestrator:
                logger.warning(
                    "Auto-backfill enabled but no backfill orchestrator available"
                )
                return

            # Create backfill request
            request = BackfillRequest(
                symbol=symbol,
                data_type="candles",
                timeframe=timeframe,
                start_time=gap.start_time,
                end_time=gap.end_time,
                priority="high" if severity == "high" else "medium",
                triggered_by="auto_gap_detection",
            )

            logger.info(
                f"Auto-triggering backfill for {symbol} {timeframe}: "
                f"{gap.start_time} to {gap.end_time} "
                f"(duration: {gap.duration_seconds}s, severity: {severity})"
            )

            # Create backfill job
            job = await self.backfill_orchestrator.create_backfill_job(request)

            logger.info(f"Backfill job created: {job.job_id} for {symbol} {timeframe}")

            # Update metrics
            auto_backfill_triggered_counter.labels(
                symbol=symbol, timeframe=timeframe, reason="gap_detected"
            ).inc()

        except Exception as e:
            logger.error(
                f"Failed to trigger auto-backfill for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
