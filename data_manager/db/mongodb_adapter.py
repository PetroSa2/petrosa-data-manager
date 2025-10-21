"""
MongoDB adapter implementation for Data Manager.

Handles time series data storage in MongoDB.
"""

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel

try:
    from motor import motor_asyncio
    from pymongo import ASCENDING, IndexModel
    from pymongo.errors import DuplicateKeyError, PyMongoError

    MOTOR_AVAILABLE = True
except ImportError:
    MOTOR_AVAILABLE = False

from data_manager.db.base_adapter import BaseAdapter, DatabaseError

logger = logging.getLogger(__name__)


class MongoDBAdapter(BaseAdapter):
    """
    MongoDB implementation of the BaseAdapter interface.

    Uses Motor async client for time series data operations.
    """

    def __init__(self, connection_string: str | None = None, **kwargs):
        """
        Initialize MongoDB adapter.

        Args:
            connection_string: MongoDB connection string
            **kwargs: Additional MongoDB client options
        """
        if not MOTOR_AVAILABLE:
            raise ImportError("Motor is required for MongoDB. Install with: pip install motor")

        super().__init__(connection_string, **kwargs)

        self.client = None
        self.db = None
        self.db_name = self._extract_db_name(connection_string)

    def _extract_db_name(self, connection_string: str) -> str:
        """Extract database name from connection string."""
        # Format: mongodb://user:pass@host:port/dbname
        if "/" in connection_string:
            parts = connection_string.split("/")
            if len(parts) > 3:
                db_name = parts[-1].split("?")[0]  # Remove query params
                return db_name if db_name else "petrosa_data_manager"
        return "petrosa_data_manager"

    def connect(self) -> None:
        """Establish connection to MongoDB."""
        try:
            self.client = motor_asyncio.AsyncIOMotorClient(self.connection_string)
            self.db = self.client[self.db_name]
            self._connected = True
            logger.info(f"Connected to MongoDB database: {self.db_name}")
        except Exception as e:
            raise DatabaseError(f"Failed to connect to MongoDB: {e}") from e

    def disconnect(self) -> None:
        """Close MongoDB connection."""
        if self.client:
            self.client.close()
            self._connected = False
            logger.info("Disconnected from MongoDB")

    async def write(self, model_instances: list[BaseModel], collection: str) -> int:
        """Write model instances to MongoDB collection."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        if not model_instances:
            return 0

        try:
            coll = self.db[collection]

            # Convert models to dictionaries
            documents = []
            for instance in model_instances:
                doc = instance.model_dump()
                # Create _id from symbol and timestamp for deduplication
                if "symbol" in doc and "timestamp" in doc:
                    timestamp_ms = int(doc["timestamp"].timestamp() * 1000)
                    doc["_id"] = f"{doc['symbol']}_{timestamp_ms}"
                documents.append(doc)

            # Insert with ordered=False to continue on duplicates
            try:
                result = await coll.insert_many(documents, ordered=False)
                return len(result.inserted_ids)
            except DuplicateKeyError:
                # Count how many were successfully inserted before duplicate
                logger.debug(f"Some duplicate records skipped in {collection}")
                return len(documents)  # Approximation

        except PyMongoError as e:
            raise DatabaseError(f"Failed to write to MongoDB {collection}: {e}") from e

    def write_batch(
        self, model_instances: list[BaseModel], collection: str, batch_size: int = 1000
    ) -> int:
        """Write model instances in batches (not implemented for async)."""
        # For MongoDB adapter, call write directly since it's async
        # Batching will be handled at a higher level
        raise NotImplementedError(
            "Use write() method directly for MongoDB. Batching should be done at caller level."
        )

    async def query_range(
        self,
        collection: str,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query records within time range."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            coll = self.db[collection]

            query = {"timestamp": {"$gte": start, "$lt": end}}
            if symbol:
                query["symbol"] = symbol

            cursor = coll.find(query).sort("timestamp", ASCENDING)
            documents = await cursor.to_list(length=None)

            # Remove _id from results
            for doc in documents:
                doc.pop("_id", None)

            return documents

        except PyMongoError as e:
            raise DatabaseError(f"Failed to query range from {collection}: {e}") from e

    async def query_latest(
        self, collection: str, symbol: str | None = None, limit: int = 1
    ) -> list[dict[str, Any]]:
        """Query most recent records."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            coll = self.db[collection]

            query = {}
            if symbol:
                query["symbol"] = symbol

            cursor = coll.find(query).sort("timestamp", -1).limit(limit)
            documents = await cursor.to_list(length=limit)

            # Remove _id from results
            for doc in documents:
                doc.pop("_id", None)

            return documents

        except PyMongoError as e:
            raise DatabaseError(f"Failed to query latest from {collection}: {e}") from e

    async def get_record_count(
        self,
        collection: str,
        start: datetime | None = None,
        end: datetime | None = None,
        symbol: str | None = None,
    ) -> int:
        """Get count of records matching criteria."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            coll = self.db[collection]

            query = {}
            if start or end:
                query["timestamp"] = {}
                if start:
                    query["timestamp"]["$gte"] = start
                if end:
                    query["timestamp"]["$lt"] = end
            if symbol:
                query["symbol"] = symbol

            count = await coll.count_documents(query)
            return count

        except PyMongoError as e:
            raise DatabaseError(f"Failed to count records in {collection}: {e}") from e

    async def ensure_indexes(self, collection: str) -> None:
        """Ensure indexes exist for collection."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            coll = self.db[collection]

            # Create indexes
            indexes = [
                IndexModel([("timestamp", ASCENDING)]),
                IndexModel([("symbol", ASCENDING), ("timestamp", ASCENDING)]),
            ]

            await coll.create_indexes(indexes)
            logger.info(f"Indexes created/verified for MongoDB collection: {collection}")

        except PyMongoError as e:
            logger.warning(f"Failed to ensure indexes for {collection}: {e}")

    async def delete_range(
        self,
        collection: str,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> int:
        """Delete records within time range."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            coll = self.db[collection]

            query = {"timestamp": {"$gte": start, "$lt": end}}
            if symbol:
                query["symbol"] = symbol

            result = await coll.delete_many(query)
            return result.deleted_count

        except PyMongoError as e:
            raise DatabaseError(f"Failed to delete from {collection}: {e}") from e

    async def list_collections(self) -> list[str]:
        """List all collections in database."""
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            collections = await self.db.list_collection_names()
            return collections
        except PyMongoError as e:
            raise DatabaseError(f"Failed to list collections: {e}") from e
