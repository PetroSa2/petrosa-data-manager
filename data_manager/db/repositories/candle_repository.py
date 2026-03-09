"""
Repository for candle/kline data operations.
"""

import logging
from datetime import datetime

import constants
from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import Candle

logger = logging.getLogger(__name__)


class CandleRepository(BaseRepository):
    """
    Repository for managing candle data.
    Supports both MongoDB (collection per pair/timeframe) and
    MySQL (one table per timeframe with symbol column).
    """

    def _get_collection_name(self, symbol: str, timeframe: str) -> str:
        """Get collection name for symbol and timeframe (MongoDB specific)."""
        return f"candles_{symbol}_{timeframe}"

    def _get_mysql_table_name(self, timeframe: str) -> str:
        """
        Map timeframe to MySQL table name.
        Converts '1h' to 'klines_h1', '15m' to 'klines_m15', etc.
        """
        if not timeframe:
            return "klines_unknown"
            
        unit = timeframe[-1]
        value = timeframe[:-1]
        return f"klines_{unit}{value}"

    async def insert(self, candle: Candle) -> bool:
        """
        Insert a single candle.

        Args:
            candle: Candle model instance

        Returns:
            True if successful, False otherwise
        """
        try:
            if constants.CANDLE_DATABASE_TYPE == "mysql":
                table = self._get_mysql_table_name(candle.timeframe)
                count = self.mysql.write([candle], table)
                return count > 0
            else:
                collection = self._get_collection_name(candle.symbol, candle.timeframe)
                count = await self.mongodb.write([candle], collection)
                return count > 0
        except Exception as e:
            logger.error(
                f"Failed to insert candle for {candle.symbol} {candle.timeframe}: {e}"
            )
            return False

    async def insert_batch(self, candles: list[Candle]) -> int:
        """
        Insert multiple candles.

        Args:
            candles: List of Candle model instances

        Returns:
            Number of candles successfully inserted
        """
        if not candles:
            return 0

        try:
            if constants.CANDLE_DATABASE_TYPE == "mysql":
                # Group candles by timeframe
                candles_by_table = {}
                for candle in candles:
                    table = self._get_mysql_table_name(candle.timeframe)
                    if table not in candles_by_table:
                        candles_by_table[table] = []
                    candles_by_table[table].append(candle)

                total_inserted = 0
                for table, table_candles in candles_by_table.items():
                    count = self.mysql.write_batch(table_candles, table)
                    total_inserted += count
                    logger.debug(f"Inserted {count} candles to {table}")
                return total_inserted
            else:
                # Group candles by symbol and timeframe
                candles_by_collection = {}
                for candle in candles:
                    collection = self._get_collection_name(
                        candle.symbol, candle.timeframe
                    )
                    if collection not in candles_by_collection:
                        candles_by_collection[collection] = []
                    candles_by_collection[collection].append(candle)

                # Insert each collection's candles
                total_inserted = 0
                for collection, collection_candles in candles_by_collection.items():
                    count = await self.mongodb.write(collection_candles, collection)
                    total_inserted += count
                    logger.debug(f"Inserted {count} candles to {collection}")

                return total_inserted

        except Exception as e:
            logger.error(f"Failed to insert candle batch: {e}")
            return 0

    async def get_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict]:
        """
        Get candles within time range.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            start: Start datetime
            end: End datetime

        Returns:
            List of candle dictionaries
        """
        try:
            if constants.CANDLE_DATABASE_TYPE == "mysql":
                table = self._get_mysql_table_name(timeframe)
                return self.mysql.query_range(table, start, end, symbol)
            else:
                collection = self._get_collection_name(symbol, timeframe)
                return await self.mongodb.query_range(collection, start, end, symbol)
        except Exception as e:
            logger.error(f"Failed to query candles for {symbol} {timeframe}: {e}")
            return []

    async def get_latest(
        self, symbol: str, timeframe: str, limit: int = 1
    ) -> list[dict]:
        """
        Get most recent candles.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            limit: Maximum number of candles to return

        Returns:
            List of candle dictionaries
        """
        try:
            if constants.CANDLE_DATABASE_TYPE == "mysql":
                table = self._get_mysql_table_name(timeframe)
                return self.mysql.query_latest(table, symbol, limit)
            else:
                collection = self._get_collection_name(symbol, timeframe)
                return await self.mongodb.query_latest(collection, symbol, limit)
        except Exception as e:
            logger.error(
                f"Failed to query latest candles for {symbol} {timeframe}: {e}"
            )
            return []

    async def count(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """
        Count candles matching criteria.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            start: Optional start datetime
            end: Optional end datetime

        Returns:
            Number of matching candles
        """
        try:
            if constants.CANDLE_DATABASE_TYPE == "mysql":
                table = self._get_mysql_table_name(timeframe)
                return self.mysql.get_record_count(table, start, end, symbol)
            else:
                collection = self._get_collection_name(symbol, timeframe)
                return await self.mongodb.get_record_count(
                    collection, start, end, symbol
                )
        except Exception as e:
            logger.error(f"Failed to count candles for {symbol} {timeframe}: {e}")
            return 0

    async def ensure_indexes(self, symbol: str, timeframe: str) -> None:
        """
        Ensure indexes exist for collection/table.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
        """
        try:
            if constants.CANDLE_DATABASE_TYPE == "mysql":
                # MySQL indexes handled during table creation
                pass
            else:
                collection = self._get_collection_name(symbol, timeframe)
                await self.mongodb.ensure_indexes(collection)
        except Exception as e:
            logger.warning(f"Failed to ensure indexes for {symbol} {timeframe}: {e}")
