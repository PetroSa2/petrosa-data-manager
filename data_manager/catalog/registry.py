"""
Dataset registry for auto-discovery and cataloging.
"""

import logging
from datetime import datetime

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CatalogRepository

logger = logging.getLogger(__name__)


class DatasetRegistry:
    """
    Auto-discovers and registers datasets from MongoDB collections.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize dataset registry.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.catalog_repo = CatalogRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def discover_and_register(self) -> int:
        """
        Discover datasets from MongoDB and register them.

        Returns:
            Number of datasets registered
        """
        try:
            logger.info("Discovering datasets from MongoDB")

            # Get all collections
            collections = await self.db_manager.mongodb_adapter.list_collections()

            datasets_registered = 0

            for coll_name in collections:
                # Parse collection name and register
                if coll_name.startswith("candles_"):
                    await self._register_candle_dataset(coll_name)
                    datasets_registered += 1
                elif coll_name.startswith("trades_"):
                    await self._register_trade_dataset(coll_name)
                    datasets_registered += 1
                elif coll_name.startswith("funding_rates_"):
                    await self._register_funding_dataset(coll_name)
                    datasets_registered += 1
                elif coll_name.startswith("depth_"):
                    await self._register_depth_dataset(coll_name)
                    datasets_registered += 1
                elif coll_name.startswith("tickers_"):
                    await self._register_ticker_dataset(coll_name)
                    datasets_registered += 1

            logger.info(f"Discovered and registered {datasets_registered} datasets")
            return datasets_registered

        except Exception as e:
            logger.error(f"Error discovering datasets: {e}", exc_info=True)
            return 0

    async def _register_candle_dataset(self, collection_name: str) -> None:
        """Register a candle dataset."""
        # Parse: candles_BTCUSDT_1m
        parts = collection_name.split("_")
        if len(parts) >= 3:
            symbol = parts[1]
            timeframe = parts[2]

            dataset = {
                "dataset_id": collection_name,
                "name": f"Candles {symbol} {timeframe}",
                "description": f"{timeframe} candles for {symbol}",
                "category": "market_data",
                "schema_id": "candle_schema_v1",
                "storage_type": "mongodb",
                "owner": "data-manager",
                "update_frequency": "real-time",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }

            await self.catalog_repo.upsert_dataset(dataset)
            logger.debug(f"Registered dataset: {collection_name}")

    async def _register_trade_dataset(self, collection_name: str) -> None:
        """Register a trade dataset."""
        # Parse: trades_BTCUSDT
        symbol = collection_name.replace("trades_", "")

        dataset = {
            "dataset_id": collection_name,
            "name": f"Trades {symbol}",
            "description": f"Individual trades for {symbol}",
            "category": "market_data",
            "schema_id": "trade_schema_v1",
            "storage_type": "mongodb",
            "owner": "data-manager",
            "update_frequency": "real-time",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        await self.catalog_repo.upsert_dataset(dataset)

    async def _register_funding_dataset(self, collection_name: str) -> None:
        """Register a funding rate dataset."""
        symbol = collection_name.replace("funding_rates_", "")

        dataset = {
            "dataset_id": collection_name,
            "name": f"Funding Rates {symbol}",
            "description": f"Funding rates for {symbol}",
            "category": "market_data",
            "schema_id": "funding_schema_v1",
            "storage_type": "mongodb",
            "owner": "data-manager",
            "update_frequency": "8h",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        await self.catalog_repo.upsert_dataset(dataset)

    async def _register_depth_dataset(self, collection_name: str) -> None:
        """Register an order book depth dataset."""
        symbol = collection_name.replace("depth_", "")

        dataset = {
            "dataset_id": collection_name,
            "name": f"Order Book Depth {symbol}",
            "description": f"Order book depth for {symbol}",
            "category": "market_data",
            "schema_id": "depth_schema_v1",
            "storage_type": "mongodb",
            "owner": "data-manager",
            "update_frequency": "real-time",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        await self.catalog_repo.upsert_dataset(dataset)

    async def _register_ticker_dataset(self, collection_name: str) -> None:
        """Register a ticker dataset."""
        symbol = collection_name.replace("tickers_", "")

        dataset = {
            "dataset_id": collection_name,
            "name": f"24h Ticker {symbol}",
            "description": f"24-hour ticker statistics for {symbol}",
            "category": "market_data",
            "schema_id": "ticker_schema_v1",
            "storage_type": "mongodb",
            "owner": "data-manager",
            "update_frequency": "real-time",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        await self.catalog_repo.upsert_dataset(dataset)
