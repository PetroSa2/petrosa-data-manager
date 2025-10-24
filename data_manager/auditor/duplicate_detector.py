"""
Duplicate detection for data quality.
"""

import logging
from datetime import datetime

from prometheus_client import Counter

import constants
from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import AuditRepository, CandleRepository

logger = logging.getLogger(__name__)

# Metrics
duplicates_removed_counter = Counter(
    "data_manager_duplicates_removed_total",
    "Total duplicate records removed",
    ["symbol", "timeframe"],
)


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
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.audit_repo = AuditRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

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
                logger.warning(
                    f"Found {duplicates} duplicate timestamps for {symbol} {timeframe}"
                )

                # Auto-remove if enabled
                if constants.ENABLE_DUPLICATE_REMOVAL:
                    removed = await self.remove_duplicates(
                        symbol, timeframe, start, end, candles
                    )
                    if removed > 0:
                        logger.info(
                            f"Removed {removed} duplicates for {symbol} {timeframe}"
                        )

            return duplicates

        except Exception as e:
            logger.error(f"Error detecting duplicates: {e}")
            return 0

    async def remove_duplicates(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        candles: list | None = None,
    ) -> int:
        """
        Remove duplicate records based on configured strategy.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe
            start: Start datetime
            end: End datetime
            candles: Optional pre-fetched candles list

        Returns:
            Number of duplicates removed
        """
        try:
            logger.info(
                f"Removing duplicates for {symbol} {timeframe} "
                f"(strategy: {constants.DUPLICATE_RESOLUTION_STRATEGY})"
            )

            # Get all candles if not provided
            if candles is None:
                candles = await self.candle_repo.get_range(
                    symbol, timeframe, start, end
                )

            # Group by timestamp
            timestamp_groups = {}
            for candle in candles:
                ts = candle.get("timestamp")
                ts_key = str(ts)
                if ts_key not in timestamp_groups:
                    timestamp_groups[ts_key] = []
                timestamp_groups[ts_key].append(candle)

            # Find and resolve duplicates
            removed_count = 0
            for ts_key, duplicate_candles in timestamp_groups.items():
                if len(duplicate_candles) <= 1:
                    continue  # No duplicates

                # Select which record to keep based on strategy
                if constants.DUPLICATE_RESOLUTION_STRATEGY == "keep_newest":
                    # Keep the candle with the most recent _id (assuming ObjectId)
                    duplicate_candles.sort(key=lambda c: c.get("_id", ""), reverse=True)
                    to_remove = duplicate_candles[1:]
                elif constants.DUPLICATE_RESOLUTION_STRATEGY == "keep_oldest":
                    # Keep the candle with the oldest _id
                    duplicate_candles.sort(key=lambda c: c.get("_id", ""))
                    to_remove = duplicate_candles[1:]
                else:
                    logger.warning(
                        f"Unknown duplicate resolution strategy: "
                        f"{constants.DUPLICATE_RESOLUTION_STRATEGY}"
                    )
                    continue

                # Remove duplicate records from MongoDB
                for candle in to_remove:
                    try:
                        candle_id = candle.get("_id")
                        if candle_id:
                            # Delete from MongoDB
                            await self.db_manager.mongodb_adapter.collection.delete_one(
                                {"_id": candle_id}
                            )
                            removed_count += 1
                            logger.debug(
                                f"Removed duplicate candle with _id: {candle_id}"
                            )
                    except Exception as e:
                        logger.error(f"Failed to remove duplicate: {e}")

                # Log duplicate removal to audit log
                await self.audit_repo.log_health_check(
                    dataset_id=f"candles_{symbol}_{timeframe}",
                    symbol=symbol,
                    details=f"Removed {len(to_remove)} duplicate records for timestamp {ts_key}",
                    severity="info",
                )

            if removed_count > 0:
                logger.info(
                    f"Successfully removed {removed_count} duplicates "
                    f"for {symbol} {timeframe}"
                )
                # Update metrics
                duplicates_removed_counter.labels(
                    symbol=symbol, timeframe=timeframe
                ).inc(removed_count)

            return removed_count

        except Exception as e:
            logger.error(f"Error removing duplicates: {e}", exc_info=True)
            return 0
