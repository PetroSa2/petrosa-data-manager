"""
Repository for order book depth data operations.
"""

import logging

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import OrderBookDepth

logger = logging.getLogger(__name__)


class DepthRepository(BaseRepository):
    """Repository for managing order book depth data in MongoDB."""

    async def insert(self, depth: OrderBookDepth) -> bool:
        """
        Insert a single depth snapshot.

        Args:
            depth: OrderBookDepth model instance

        Returns:
            True if successful, False otherwise
        """
        try:
            collection = f"depth_{depth.symbol}"
            count = await self.mongodb.write([depth], collection)
            return count > 0
        except Exception as e:
            # Only log as error if it's not a duplicate key issue
            if "duplicate key error" in str(e).lower():
                logger.debug(f"Skipped duplicate depth for {depth.symbol}")
                return False
            logger.error(f"Failed to insert depth for {depth.symbol}: {e}")
            return False

    async def insert_batch(self, depths: list[OrderBookDepth]) -> int:
        """
        Insert multiple depth snapshots.

        Args:
            depths: List of OrderBookDepth model instances

        Returns:
            Number of depths successfully inserted
        """
        if not depths:
            return 0

        try:
            # Group depths by symbol
            depths_by_symbol = {}
            for depth in depths:
                if depth.symbol not in depths_by_symbol:
                    depths_by_symbol[depth.symbol] = []
                depths_by_symbol[depth.symbol].append(depth)

            # Insert each symbol's depths to its collection
            total_inserted = 0
            for symbol, symbol_depths in depths_by_symbol.items():
                collection = f"depth_{symbol}"
                count = await self.mongodb.write(symbol_depths, collection)
                total_inserted += count
                logger.debug(f"Inserted {count} depth snapshots for {symbol}")

            return total_inserted

        except Exception as e:
            # Only log as error if it's not a duplicate key issue
            if "duplicate key error" in str(e).lower():
                logger.debug("Skipped some duplicate depths in batch")
                return total_inserted  # Return what was inserted
            logger.error(f"Failed to insert depth batch: {e}")
            return 0

    async def get_latest(self, symbol: str, limit: int = 1) -> list[dict]:
        """
        Get most recent depth snapshots.

        Args:
            symbol: Trading pair symbol
            limit: Maximum number of snapshots to return

        Returns:
            List of depth dictionaries
        """
        try:
            collection = f"depth_{symbol}"
            return await self.mongodb.query_latest(collection, symbol, limit)
        except Exception as e:
            logger.error(f"Failed to query latest depth for {symbol}: {e}")
            return []
