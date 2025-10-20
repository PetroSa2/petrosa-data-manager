"""
Database manager to coordinate MySQL and MongoDB adapters.
"""

import logging

import constants
from data_manager.db import get_adapter
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages both MySQL and MongoDB database connections.

    Provides unified interface for database operations across both adapters.
    """

    def __init__(self):
        """Initialize database manager."""
        self.mysql_adapter: MySQLAdapter | None = None
        self.mongodb_adapter: MongoDBAdapter | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize both database adapters."""
        try:
            logger.info("Initializing database connections...")

            # Initialize MySQL adapter (synchronous)
            logger.info("Connecting to MySQL...")
            self.mysql_adapter = get_adapter("mysql", constants.MYSQL_URI)
            self.mysql_adapter.connect()
            logger.info("MySQL connection established")

            # Initialize MongoDB adapter (async)
            logger.info("Connecting to MongoDB...")
            self.mongodb_adapter = get_adapter("mongodb", constants.MONGODB_URL)
            self.mongodb_adapter.connect()
            logger.info("MongoDB connection established")

            self._initialized = True
            logger.info("All database connections initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize databases: {e}")
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        """Shutdown all database connections."""
        logger.info("Shutting down database connections...")

        if self.mysql_adapter:
            try:
                self.mysql_adapter.disconnect()
                logger.info("MySQL disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting MySQL: {e}")

        if self.mongodb_adapter:
            try:
                self.mongodb_adapter.disconnect()
                logger.info("MongoDB disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting MongoDB: {e}")

        self._initialized = False
        logger.info("All database connections closed")

    def health_check(self) -> dict:
        """
        Check health status of all database connections.

        Returns:
            Dictionary with connection status for each database
        """
        return {
            "mysql": {
                "connected": self.mysql_adapter.is_connected() if self.mysql_adapter else False,
                "type": "mysql",
            },
            "mongodb": {
                "connected": (
                    self.mongodb_adapter.is_connected() if self.mongodb_adapter else False
                ),
                "type": "mongodb",
            },
            "initialized": self._initialized,
        }

    def is_healthy(self) -> bool:
        """Check if all databases are connected."""
        health = self.health_check()
        return health["mysql"]["connected"] and health["mongodb"]["connected"]

    def __enter__(self):
        """Context manager entry."""
        # Note: This is synchronous, async context manager should use __aenter__
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        # For async, this should be __aexit__
        pass

    async def __aenter__(self):
        """Async context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.shutdown()
