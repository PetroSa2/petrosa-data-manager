"""
Health scoring for datasets.
"""

import logging
from datetime import datetime, timedelta

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository, HealthRepository
from data_manager.models.health import DataHealthMetrics
from data_manager.utils.time_utils import calculate_expected_records

logger = logging.getLogger(__name__)


class HealthScorer:
    """
    Calculates health scores for datasets.

    Computes completeness, freshness, and overall quality scores.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize health scorer.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.health_repo = HealthRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_health(
        self,
        symbol: str,
        timeframe: str,
        lookback_hours: int = 24,
        gaps: list | None = None,
        duplicates_count: int = 0,
    ) -> DataHealthMetrics:
        """
        Calculate health metrics for a dataset.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            lookback_hours: Hours to look back for analysis
            gaps: List of detected gaps (optional)
            duplicates_count: Number of duplicates detected

        Returns:
            DataHealthMetrics instance
        """
        try:
            end = datetime.utcnow()
            start = end - timedelta(hours=lookback_hours)

            # Get actual record count
            actual_count = await self.candle_repo.count(symbol, timeframe, start, end)

            # Calculate expected record count
            expected_count = calculate_expected_records(start, end, timeframe)

            # Calculate completeness
            completeness = (
                (actual_count / expected_count * 100) if expected_count > 0 else 0.0
            )

            # Get freshness (seconds since last data point)
            latest_candles = await self.candle_repo.get_latest(
                symbol, timeframe, limit=1
            )
            if latest_candles:
                latest_timestamp = latest_candles[0].get("timestamp")
                if isinstance(latest_timestamp, str):
                    latest_timestamp = datetime.fromisoformat(latest_timestamp)
                freshness_seconds = int((end - latest_timestamp).total_seconds())
            else:
                freshness_seconds = int(timedelta(hours=lookback_hours).total_seconds())

            # Use provided gaps and duplicates count
            gaps_count = len(gaps) if gaps else 0

            # Calculate consistency score based on data quality issues
            consistency_score = 100.0

            # Reduce score for gaps (each gap reduces score)
            if gaps_count > 0:
                gap_penalty = min(gaps_count * 10, 50)  # Max 50% penalty for gaps
                consistency_score -= gap_penalty

            # Reduce score for duplicates
            if duplicates_count > 0:
                duplicate_penalty = min(
                    duplicates_count * 5, 30
                )  # Max 30% penalty for duplicates
                consistency_score -= duplicate_penalty

            # Ensure consistency score is not negative
            consistency_score = max(consistency_score, 0.0)

            # Calculate freshness score (0-100, where 100 is most recent)
            # Penalize data older than 5 minutes (300 seconds)
            freshness_score = max(0.0, 100.0 - (freshness_seconds / 300.0 * 100.0))
            freshness_score = min(freshness_score, 100.0)

            # Calculate overall quality score (weighted average)
            quality_score = (
                completeness * 0.4  # 40% weight on completeness
                + consistency_score * 0.4  # 40% weight on consistency
                + freshness_score * 0.2  # 20% weight on freshness
            )

            metrics = DataHealthMetrics(
                completeness=completeness,
                freshness_seconds=freshness_seconds,
                gaps_count=gaps_count,
                duplicates_count=duplicates_count,
                consistency_score=consistency_score,
                quality_score=quality_score,
            )

            # Store health metrics
            dataset_id = f"candles_{symbol}_{timeframe}"
            await self.health_repo.insert(dataset_id, symbol, metrics)

            logger.debug(
                f"Health calculated for {symbol} {timeframe}: "
                f"completeness={completeness:.2f}%, "
                f"consistency={consistency_score:.2f}%, "
                f"freshness={freshness_score:.2f}%, "
                f"quality={quality_score:.2f}, "
                f"gaps={gaps_count}, "
                f"duplicates={duplicates_count}"
            )

            return metrics

        except Exception as e:
            logger.error(
                f"Error calculating health for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
            # Return default metrics on error
            return DataHealthMetrics(
                completeness=0.0,
                freshness_seconds=86400,  # 1 day
                gaps_count=0,
                duplicates_count=0,
                consistency_score=0.0,
                quality_score=0.0,
            )
