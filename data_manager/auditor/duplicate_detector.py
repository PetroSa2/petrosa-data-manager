"""
Duplicate detection for data quality.
"""

import logging
from datetime import datetime

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository

logger = logging.getLogger(__name__)


class DuplicateDetector:
    """
    Detects duplicate records in time series data.

    Note: MongoDB's _id based on {symbol}_{timestamp_ms} prevents most duplicates.
    This detector finds logical duplicates that may slip through.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize duplicate detector.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(db_manager.mysql_adapter, db_manager.mongodb_adapter)

    async def detect_duplicates(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> int:
        """
        Detect duplicate records.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe
            start: Start datetime
            end: End datetime

        Returns:
            Number of duplicates found
        """
        try:
            logger.debug(f"Checking for duplicates in {symbol} {timeframe}")

            # Get all candles
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            # Group by timestamp
            timestamp_counts = {}
            for candle in candles:
                ts = candle.get("timestamp")
                ts_key = str(ts)
                timestamp_counts[ts_key] = timestamp_counts.get(ts_key, 0) + 1

            # Count duplicates
            duplicates = sum(1 for count in timestamp_counts.values() if count > 1)

            if duplicates > 0:
                logger.warning(f"Found {duplicates} duplicate timestamps for {symbol} {timeframe}")

            return duplicates

        except Exception as e:
            logger.error(f"Error detecting duplicates: {e}")
            return 0
