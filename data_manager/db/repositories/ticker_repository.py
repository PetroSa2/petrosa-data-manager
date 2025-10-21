"""
Repository for ticker data operations.
"""

import logging

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import Ticker

logger = logging.getLogger(__name__)


class TickerRepository(BaseRepository):
    """Repository for managing ticker data in MongoDB."""

    async def insert(self, ticker: Ticker) -> bool:
        """
        Insert a single ticker.

        Args:
            ticker: Ticker model instance

        Returns:
            True if successful, False otherwise
        """
        try:
            collection = f"tickers_{ticker.symbol}"
            count = await self.mongodb.write([ticker], collection)
            return count > 0
        except Exception as e:
            logger.error(f"Failed to insert ticker for {ticker.symbol}: {e}")
            return False

    async def insert_batch(self, tickers: list[Ticker]) -> int:
        """
        Insert multiple tickers.

        Args:
            tickers: List of Ticker model instances

        Returns:
            Number of tickers successfully inserted
        """
        if not tickers:
            return 0

        try:
            # Group by symbol
            tickers_by_symbol = {}
            for ticker in tickers:
                if ticker.symbol not in tickers_by_symbol:
                    tickers_by_symbol[ticker.symbol] = []
                tickers_by_symbol[ticker.symbol].append(ticker)

            # Insert each symbol's tickers to its collection
            total_inserted = 0
            for symbol, symbol_tickers in tickers_by_symbol.items():
                collection = f"tickers_{symbol}"
                count = await self.mongodb.write(symbol_tickers, collection)
                total_inserted += count
                logger.debug(f"Inserted {count} tickers for {symbol}")

            return total_inserted

        except Exception as e:
            logger.error(f"Failed to insert ticker batch: {e}")
            return 0

    async def get_latest(self, symbol: str, limit: int = 1) -> list[dict]:
        """
        Get most recent tickers.

        Args:
            symbol: Trading pair symbol
            limit: Maximum number of tickers to return

        Returns:
            List of ticker dictionaries
        """
        try:
            collection = f"tickers_{symbol}"
            return await self.mongodb.query_latest(collection, symbol, limit)
        except Exception as e:
            logger.error(f"Failed to query latest tickers for {symbol}: {e}")
            return []
