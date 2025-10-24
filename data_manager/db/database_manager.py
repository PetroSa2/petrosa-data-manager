"""
Database manager to coordinate MySQL and MongoDB adapters.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import constants
from data_manager.db import get_adapter
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages both MySQL and MongoDB database connections.

    Provides unified interface for database operations across both adapters.
    Enhanced with health monitoring, auto-reconnection, and connection metrics.
    """

    def __init__(self):
        """Initialize database manager."""
        self.mysql_adapter: MySQLAdapter | None = None
        self.mongodb_adapter: MongoDBAdapter | None = None
        self._initialized = False

        # Connection metrics
        self._connection_start_time = None
        self._mysql_reconnect_attempts = 0
        self._mongodb_reconnect_attempts = 0
        self._last_health_check = None
        self._health_check_task = None
        self._shutdown_event = asyncio.Event()

        # Connection statistics
        self._stats = {
            "mysql": {
                "connection_count": 0,
                "last_connected": None,
                "last_disconnected": None,
                "reconnect_attempts": 0,
                "query_count": 0,
                "error_count": 0,
            },
            "mongodb": {
                "connection_count": 0,
                "last_connected": None,
                "last_disconnected": None,
                "reconnect_attempts": 0,
                "query_count": 0,
                "error_count": 0,
            },
        }

    async def initialize(self) -> None:
        """Initialize both database adapters with health monitoring."""
        try:
            logger.info("Initializing database connections...")
            self._connection_start_time = time.time()

            # Initialize MySQL adapter (synchronous)
            logger.info("Connecting to MySQL...")
            self.mysql_adapter = get_adapter("mysql", constants.MYSQL_URI)
            self.mysql_adapter.connect()
            self._stats["mysql"]["connection_count"] += 1
            self._stats["mysql"]["last_connected"] = datetime.utcnow()
            logger.info("MySQL connection established")

            # Initialize MongoDB adapter (async)
            logger.info("Connecting to MongoDB...")
            self.mongodb_adapter = get_adapter("mongodb", constants.MONGODB_URL)
            self.mongodb_adapter.connect()
            self._stats["mongodb"]["connection_count"] += 1
            self._stats["mongodb"]["last_connected"] = datetime.utcnow()
            logger.info("MongoDB connection established")

            self._initialized = True
            logger.info("All database connections initialized successfully")

            # Start health monitoring task
            self._health_check_task = asyncio.create_task(self._health_monitor())
            logger.info("Health monitoring started")

        except Exception as e:
            logger.error(f"Failed to initialize databases: {e}")
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        """Shutdown all database connections."""
        logger.info("Shutting down database connections...")

        # Stop health monitoring
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            logger.info("Health monitoring stopped")

        if self.mysql_adapter:
            try:
                self.mysql_adapter.disconnect()
                self._stats["mysql"]["last_disconnected"] = datetime.utcnow()
                logger.info("MySQL disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting MySQL: {e}")

        if self.mongodb_adapter:
            try:
                self.mongodb_adapter.disconnect()
                self._stats["mongodb"]["last_disconnected"] = datetime.utcnow()
                logger.info("MongoDB disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting MongoDB: {e}")

        self._initialized = False
        self._shutdown_event.set()
        logger.info("All database connections closed")

    def health_check(self) -> dict:
        """
        Check health status of all database connections.

        Returns:
            Dictionary with connection status for each database
        """
        mysql_connected = (
            self.mysql_adapter.is_connected() if self.mysql_adapter else False
        )
        mongodb_connected = (
            self.mongodb_adapter.is_connected() if self.mongodb_adapter else False
        )

        return {
            "mysql": {
                "connected": mysql_connected,
                "type": "mysql",
                "stats": self._stats["mysql"],
                "uptime_seconds": (
                    time.time() - self._connection_start_time
                    if self._connection_start_time
                    else 0
                ),
            },
            "mongodb": {
                "connected": mongodb_connected,
                "type": "mongodb",
                "stats": self._stats["mongodb"],
                "uptime_seconds": (
                    time.time() - self._connection_start_time
                    if self._connection_start_time
                    else 0
                ),
            },
            "initialized": self._initialized,
            "last_health_check": self._last_health_check,
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

    async def _health_monitor(self) -> None:
        """Background health monitoring with auto-reconnection."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(constants.DB_HEALTH_CHECK_INTERVAL)
                self._last_health_check = datetime.utcnow()

                # Check MySQL connection
                if self.mysql_adapter and not self.mysql_adapter.is_connected():
                    logger.warning("MySQL connection lost, attempting reconnection...")
                    await self._reconnect_mysql()

                # Check MongoDB connection
                if self.mongodb_adapter and not self.mongodb_adapter.is_connected():
                    logger.warning(
                        "MongoDB connection lost, attempting reconnection..."
                    )
                    await self._reconnect_mongodb()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health monitoring: {e}")

    async def _reconnect_mysql(self) -> None:
        """Reconnect to MySQL with exponential backoff."""
        if self._mysql_reconnect_attempts >= constants.DB_RECONNECT_MAX_ATTEMPTS:
            logger.error("Max MySQL reconnection attempts reached")
            return

        try:
            self._mysql_reconnect_attempts += 1
            self._stats["mysql"]["reconnect_attempts"] += 1

            # Exponential backoff
            backoff_delay = (
                constants.DB_RECONNECT_BACKOFF_BASE**self._mysql_reconnect_attempts
            )
            await asyncio.sleep(backoff_delay)

            # Attempt reconnection
            self.mysql_adapter = get_adapter("mysql", constants.MYSQL_URI)
            self.mysql_adapter.connect()

            self._stats["mysql"]["connection_count"] += 1
            self._stats["mysql"]["last_connected"] = datetime.utcnow()
            self._mysql_reconnect_attempts = 0  # Reset on success

            logger.info("MySQL reconnection successful")

        except Exception as e:
            self._stats["mysql"]["error_count"] += 1
            logger.error(
                f"MySQL reconnection failed (attempt {self._mysql_reconnect_attempts}): {e}"
            )

    async def _reconnect_mongodb(self) -> None:
        """Reconnect to MongoDB with exponential backoff."""
        if self._mongodb_reconnect_attempts >= constants.DB_RECONNECT_MAX_ATTEMPTS:
            logger.error("Max MongoDB reconnection attempts reached")
            return

        try:
            self._mongodb_reconnect_attempts += 1
            self._stats["mongodb"]["reconnect_attempts"] += 1

            # Exponential backoff
            backoff_delay = (
                constants.DB_RECONNECT_BACKOFF_BASE**self._mongodb_reconnect_attempts
            )
            await asyncio.sleep(backoff_delay)

            # Attempt reconnection
            self.mongodb_adapter = get_adapter("mongodb", constants.MONGODB_URL)
            self.mongodb_adapter.connect()

            self._stats["mongodb"]["connection_count"] += 1
            self._stats["mongodb"]["last_connected"] = datetime.utcnow()
            self._mongodb_reconnect_attempts = 0  # Reset on success

            logger.info("MongoDB reconnection successful")

        except Exception as e:
            self._stats["mongodb"]["error_count"] += 1
            logger.error(
                f"MongoDB reconnection failed (attempt {self._mongodb_reconnect_attempts}): {e}"
            )

    def get_connection_stats(self) -> dict[str, Any]:
        """Get detailed connection statistics."""
        return {
            "overall": {
                "initialized": self._initialized,
                "uptime_seconds": (
                    time.time() - self._connection_start_time
                    if self._connection_start_time
                    else 0
                ),
                "last_health_check": self._last_health_check,
            },
            "databases": self._stats,
        }

    def increment_query_count(self, database: str) -> None:
        """Increment query count for a database."""
        if database in self._stats:
            self._stats[database]["query_count"] += 1

    def increment_error_count(self, database: str) -> None:
        """Increment error count for a database."""
        if database in self._stats:
            self._stats[database]["error_count"] += 1
