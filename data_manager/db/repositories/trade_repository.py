"""
Repository for trade data operations.
"""

import logging
from datetime import datetime

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import Trade

logger = logging.getLogger(__name__)


class TradeRepository(BaseRepository):
    """Repository for managing trade data in MongoDB."""

    async def insert(self, trade: Trade) -> bool:
        """
        Insert a single trade.

        Args:
            trade: Trade model instance

        Returns:
            True if successful, False otherwise
        """
        try:
            collection = f"trades_{trade.symbol}"
            count = await self.mongodb.write([trade], collection)
            return count > 0
        except Exception as e:
            logger.error(f"Failed to insert trade for {trade.symbol}: {e}")
            return False

    async def insert_batch(self, trades: list[Trade]) -> int:
        """
        Insert multiple trades.

        Args:
            trades: List of Trade model instances

        Returns:
            Number of trades successfully inserted
        """
        if not trades:
            return 0

        try:
            # Group trades by symbol
            trades_by_symbol = {}
            for trade in trades:
                if trade.symbol not in trades_by_symbol:
                    trades_by_symbol[trade.symbol] = []
                trades_by_symbol[trade.symbol].append(trade)

            # Insert each symbol's trades to its collection
            total_inserted = 0
            for symbol, symbol_trades in trades_by_symbol.items():
                collection = f"trades_{symbol}"
                count = await self.mongodb.write(symbol_trades, collection)
                total_inserted += count
                logger.debug(f"Inserted {count} trades for {symbol}")

            return total_inserted

        except Exception as e:
            logger.error(f"Failed to insert trade batch: {e}")
            return 0

    async def get_range(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict]:
        """
        Get trades within time range.

        Args:
            symbol: Trading pair symbol
            start: Start datetime
            end: End datetime

        Returns:
            List of trade dictionaries
        """
        try:
            collection = f"trades_{symbol}"
            return await self.mongodb.query_range(collection, start, end, symbol)
        except Exception as e:
            logger.error(f"Failed to query trades for {symbol}: {e}")
            return []

    async def get_latest(self, symbol: str, limit: int = 1) -> list[dict]:
        """
        Get most recent trades.

        Args:
            symbol: Trading pair symbol
            limit: Maximum number of trades to return

        Returns:
            List of trade dictionaries
        """
        try:
            collection = f"trades_{symbol}"
            return await self.mongodb.query_latest(collection, symbol, limit)
        except Exception as e:
            logger.error(f"Failed to query latest trades for {symbol}: {e}")
            return []

    async def count(
        self, symbol: str, start: datetime | None = None, end: datetime | None = None
    ) -> int:
        """
        Count trades matching criteria.

        Args:
            symbol: Trading pair symbol
            start: Optional start datetime
            end: Optional end datetime

        Returns:
            Number of matching trades
        """
        try:
            collection = f"trades_{symbol}"
            return await self.mongodb.get_record_count(collection, start, end, symbol)
        except Exception as e:
            logger.error(f"Failed to count trades for {symbol}: {e}")
            return 0
