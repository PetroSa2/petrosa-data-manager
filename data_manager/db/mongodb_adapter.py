"""
MongoDB adapter implementation for Data Manager.

Handles time series data storage in MongoDB.
"""

import logging
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017
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
            raise ImportError(
                "Motor is required for MongoDB. Install with: pip install motor"
            )

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
                    ts = doc["timestamp"]
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts)
                        # Normalize back into document for correct storage type
                        doc["timestamp"] = ts
                    timestamp_ms = int(ts.timestamp() * 1000)
                    doc["_id"] = f"{doc['symbol']}_{timestamp_ms}"

                # Convert Decimal to float for MongoDB compatibility
                doc = self._prepare_for_bson(doc)
                documents.append(doc)

            # Insert with ordered=False to continue on duplicates
            try:
                result = await coll.insert_many(documents, ordered=False)
                return len(result.inserted_ids)
            except DuplicateKeyError as e:
                # Extract actual inserted count from error details
                # This is expected behavior - duplicates are normal during backfill/replay
                inserted_count = 0
                if hasattr(e, "details") and e.details:
                    inserted_count = e.details.get("nInserted", 0)
                # Only log if we actually inserted something, otherwise it's just duplicates
                if inserted_count > 0:
                    logger.debug(
                        f"Inserted {inserted_count}/{len(documents)} records to {collection} "
                        f"(skipped {len(documents) - inserted_count} duplicates)"
                    )
                return inserted_count if inserted_count > 0 else 0

        except PyMongoError as e:
            # Check if this is a duplicate key error wrapped in another exception
            if "duplicate key error" in str(e).lower():
                # Duplicates are expected - no need to log
                return 0
            # Only raise for actual errors, not duplicates
            logger.warning(f"MongoDB write error for {collection}: {e}")
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

    async def find_filtered(
        self,
        collection: str,
        *,
        filters: dict[str, Any] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        sort_field: str = "timestamp",
        sort_order: int = -1,
    ) -> list[dict[str, Any]]:
        """Query a collection with a composable equality + time-window filter.

        The cross-service audit-trail surface (#605 P4.5) needs to filter
        ``cio_decisions`` by arbitrary indexed fields (``strategy_id``,
        ``action``, ``decision_id``) without bolting more named parameters
        onto :meth:`query_latest`. ``filters`` accepts ``{column: value}``
        pairs and is composed with the optional ``[start, end)``
        time-window via Mongo's ``$gte`` / ``$lt`` operators.

        Args:
            collection: target collection name.
            filters: equality filters as ``{field: value}``; ``None``
                values are skipped so callers can pass through optional
                request params without branching.
            start: inclusive lower bound on ``sort_field`` (typically
                ``timestamp``).
            end: exclusive upper bound on ``sort_field``.
            limit: hard cap on returned documents (caller enforces sanity).
            sort_field: field to sort + window on (defaults to ``timestamp``).
            sort_order: 1 (ASC) or -1 (DESC, default — newest first).
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            coll = self.db[collection]

            query: dict[str, Any] = {}
            if filters:
                for field, value in filters.items():
                    if value is None:
                        continue
                    query[field] = value
            if start is not None or end is not None:
                window: dict[str, Any] = {}
                if start is not None:
                    window["$gte"] = start
                if end is not None:
                    window["$lt"] = end
                if window:
                    query[sort_field] = window

            cursor = coll.find(query).sort(sort_field, sort_order).limit(limit)
            documents = await cursor.to_list(length=limit)

            for doc in documents:
                doc.pop("_id", None)

            return documents

        except PyMongoError as e:
            raise DatabaseError(
                f"Failed to query {collection} with filters: {e}"
            ) from e

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

            # Create indexes based on collection type
            if collection == "schemas":
                # Schema registry specific indexes
                indexes = [
                    IndexModel([("name", ASCENDING)]),
                    IndexModel([("version", ASCENDING)]),
                    IndexModel([("status", ASCENDING)]),
                    IndexModel(
                        [("name", ASCENDING), ("version", ASCENDING)], unique=True
                    ),
                    IndexModel([("created_at", ASCENDING)]),
                ]
            elif collection == "intents":
                # Cross-service identifier contract (P0.2a): `intents` collection
                # decision_id is sparse — publishers may emit before CIO assigns one.
                indexes = [
                    IndexModel([("intent_id", ASCENDING)], unique=True),
                    IndexModel([("strategy_id", ASCENDING)]),
                    IndexModel([("decision_id", ASCENDING)], sparse=True),
                    IndexModel([("timestamp", ASCENDING)]),
                ]
            elif collection == "cio_decisions":
                # Cross-service identifier contract (P0.2b): `cio_decisions` collection
                # CIO has assigned decision_id by the time it publishes onto
                # signals.trading.> — so decision_id is the unique key.
                indexes = [
                    IndexModel([("decision_id", ASCENDING)], unique=True),
                    IndexModel([("strategy_id", ASCENDING)]),
                    IndexModel([("timestamp", ASCENDING)]),
                    IndexModel([("action", ASCENDING)]),
                ]
            elif collection == "execution_events":
                # Cross-service identifier contract (P0.2c): `execution_events` collection
                # The same order_id emits multiple events (placed → filled, etc.)
                # so neither order_id alone nor decision_id alone is unique;
                # uniqueness is enforced via the synthesized `_id`
                # (order_id:event_type) on insert.
                indexes = [
                    IndexModel([("decision_id", ASCENDING)]),
                    IndexModel([("order_id", ASCENDING)]),
                    IndexModel([("strategy_id", ASCENDING)]),
                    IndexModel([("timestamp", ASCENDING)]),
                    IndexModel([("event_type", ASCENDING)]),
                ]
            elif collection == "pnl_events":
                # Cross-service identifier contract (P0.2d): `pnl_events` collection.
                # A single decision can produce many P&L events over time
                # (closed + repeated mark_to_market snapshots), so decision_id
                # alone is not unique; uniqueness is enforced via the
                # synthesized `_id` (decision_id:pnl_kind:timestamp_micros)
                # on insert.
                indexes = [
                    IndexModel([("decision_id", ASCENDING)]),
                    IndexModel([("strategy_id", ASCENDING)]),
                    IndexModel([("timestamp", ASCENDING)]),
                    IndexModel([("pnl_kind", ASCENDING)]),
                    IndexModel([("order_id", ASCENDING)], sparse=True),
                ]
            else:
                # Default time-series indexes
                indexes = [
                    IndexModel([("timestamp", ASCENDING)]),
                    IndexModel([("symbol", ASCENDING), ("timestamp", ASCENDING)]),
                ]

            await coll.create_indexes(indexes)
            logger.info(
                f"Indexes created/verified for MongoDB collection: {collection}"
            )

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

    # -------------------------------------------------------------------------
    # Read-only introspection helpers (petrosa_k8s#794 storage-inventory audit)
    #
    # These methods issue ONLY MongoDB read commands (listDatabases, dbStats,
    # collStats, list_collections with filter, find_one). They never write,
    # update, drop, or otherwise mutate the cluster — and they accept an
    # explicit `db_name` so the storage-inventory audit can iterate every
    # database returned by `listDatabases`, not just the connected default.
    # -------------------------------------------------------------------------

    async def list_databases(self) -> list[dict[str, Any]]:
        """Return every database the client can see via ``listDatabases``.

        Includes ``local``/``admin``/``config`` system DBs because the audit
        cares about oplog and system-DB storage too. Each entry has at least
        ``name``, ``sizeOnDisk``, ``empty``.
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            cursor = await self.client.list_databases()
            return await cursor.to_list(length=None)
        except PyMongoError as e:
            raise DatabaseError(f"Failed to list databases: {e}") from e

    async def db_stats(self, db_name: str) -> dict[str, Any]:
        """Return ``dbStats`` for ``db_name``.

        The verdict figure for Atlas-quota attribution is ``storageSize``
        (compressed on-disk), NOT ``dataSize`` (logical). Both are returned
        so callers can report each explicitly.
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            stats = await self.client[db_name].command("dbStats")
            return dict(stats)
        except PyMongoError as e:
            raise DatabaseError(f"Failed to fetch dbStats for {db_name}: {e}") from e

    async def coll_stats(self, db_name: str, collection: str) -> dict[str, Any]:
        """Return ``collStats`` for one collection.

        Falls back to a ``$collStats`` aggregation if the legacy ``collStats``
        command is restricted by Atlas role — the aggregation surface has
        broader cluster-monitor reach.
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        target_db = self.client[db_name]
        try:
            stats = await target_db.command("collStats", collection)
            return dict(stats)
        except PyMongoError as exc:
            try:
                cursor = target_db[collection].aggregate(
                    [{"$collStats": {"storageStats": {}}}]
                )
                docs = await cursor.to_list(length=1)
                if docs:
                    storage = dict(docs[0].get("storageStats", {}))
                    storage["_via_aggregation"] = True
                    return storage
            except PyMongoError:
                pass
            raise DatabaseError(
                f"Failed to fetch collStats for {db_name}.{collection}: {exc}"
            ) from exc

    async def is_timeseries(self, db_name: str, collection: str) -> bool:
        """Return True when ``collection`` is a MongoDB time-series collection.

        Time-series collections store their actual bytes in a hidden backing
        ``system.buckets.<name>``; the audit must size BOTH the logical name
        and the backing bucket or it under/over-counts ``klines_*``.
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        try:
            target_db = self.client[db_name]
            spec = await target_db.list_collections(
                filter={"name": collection}
            ).to_list(length=1)
        except PyMongoError as exc:
            raise DatabaseError(
                f"Failed to read collection spec for {db_name}.{collection}: {exc}"
            ) from exc

        if not spec:
            return False
        info = spec[0]
        if info.get("type") == "timeseries":
            return True
        options = info.get("options") or {}
        return "timeseries" in options

    async def oplog_size(self) -> dict[str, Any]:
        """Return ``collStats`` for ``local.oplog.rs`` (capped; invisible to the app)."""
        if not self._connected:
            raise DatabaseError("Not connected to database")
        return await self.coll_stats("local", "oplog.rs")

    async def newest_doc_age(self, db_name: str, collection: str) -> datetime | None:
        """Best-effort newest-document timestamp across common time fields.

        Tries ``timestamp``, ``time``, ``open_time``, ``created_at``, ``_id``
        in order; returns the first hit or ``None`` if the collection is
        empty / has no recognized time field.
        """
        if not self._connected:
            raise DatabaseError("Not connected to database")

        candidate_fields = ("timestamp", "time", "open_time", "created_at", "_id")
        coll = self.client[db_name][collection]
        for field in candidate_fields:
            try:
                doc = await coll.find_one(sort=[(field, -1)])
            except PyMongoError:
                continue
            if not doc:
                return None
            value = doc.get(field)
            if isinstance(value, datetime):
                return value
            if field == "_id" and value is not None:
                generation = getattr(value, "generation_time", None)
                if isinstance(generation, datetime):
                    return generation
        return None

    @staticmethod
    def _prepare_for_bson(doc: dict) -> dict:
        """
        Recursively convert Decimal objects to float and ensure keys are strings.

        MongoDB Motor doesn't support Decimal type, and BSON requires string keys.
        """
        from decimal import Decimal

        new_doc = {}
        for key, value in doc.items():
            str_key = str(key)
            if str_key in new_doc:
                logger.warning(
                    "Key collision detected during BSON preparation: '%s' "
                    "already exists in document. Data may be overwritten.",
                    str_key,
                )

            if isinstance(value, Decimal):
                new_doc[str_key] = float(value)
            elif isinstance(value, dict):
                new_doc[str_key] = MongoDBAdapter._prepare_for_bson(value)
            elif isinstance(value, list):
                new_doc[str_key] = [
                    (
                        MongoDBAdapter._prepare_for_bson(item)
                        if isinstance(item, dict)
                        else float(item)
                        if isinstance(item, Decimal)
                        else item
                    )
                    for item in value
                ]
            else:
                new_doc[str_key] = value
        return new_doc

    # -------------------------------------------------------------------------
    # Configuration Management Methods
    # -------------------------------------------------------------------------

    async def get_app_config(self) -> dict | None:
        """
        Get application configuration.

        Returns:
            Configuration document or None if not found
        """
        if not self._connected:
            return None

        try:
            # Single document approach - get the first (and only) config
            config = await self.db.app_config.find_one()
            return config
        except Exception as e:
            logger.error(f"Error fetching app config: {e}")
            return None

    async def upsert_app_config(self, config: dict, metadata: dict) -> str | None:
        """
        Create or update application configuration.

        Args:
            config: Configuration values
            metadata: Additional metadata (created_by, reason, etc.)

        Returns:
            Configuration ID or None on failure
        """
        if not self._connected:
            return None

        try:
            # Add metadata
            config.update(metadata)
            config["updated_at"] = datetime.now(UTC).isoformat()

            # Upsert (update if exists, insert if not)
            result = await self.db.app_config.replace_one(
                {},
                config,
                upsert=True,  # Empty filter matches any document
            )

            if result.upserted_id:
                logger.info(f"Created new app config: {result.upserted_id}")
                return str(result.upserted_id)
            else:
                logger.info("Updated existing app config")
                return "updated"

        except Exception as e:
            logger.error(f"Error upserting app config: {e}")
            return None

    async def get_global_config(self, strategy_id: str) -> dict | None:
        """
        Get global strategy configuration.

        Args:
            strategy_id: Strategy identifier

        Returns:
            Configuration document or None if not found
        """
        if not self._connected:
            return None

        try:
            config = await self.db.strategy_configs_global.find_one(
                {"strategy_id": strategy_id}
            )
            return config
        except Exception as e:
            logger.error(f"Error fetching global config for {strategy_id}: {e}")
            return None

    async def upsert_global_config(
        self, strategy_id: str, parameters: dict, metadata: dict
    ) -> str | None:
        """
        Create or update global strategy configuration.

        Args:
            strategy_id: Strategy identifier
            parameters: Strategy parameters
            metadata: Additional metadata

        Returns:
            Configuration ID or None on failure
        """
        if not self._connected:
            return None

        try:
            config_doc = {
                "strategy_id": strategy_id,
                "parameters": parameters,
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                **metadata,
            }

            result = await self.db.strategy_configs_global.replace_one(
                {"strategy_id": strategy_id}, config_doc, upsert=True
            )

            if result.upserted_id:
                logger.info(
                    f"Created global config for {strategy_id}: {result.upserted_id}"
                )
                return str(result.upserted_id)
            else:
                logger.info(f"Updated global config for {strategy_id}")
                return "updated"

        except Exception as e:
            logger.error(f"Error upserting global config for {strategy_id}: {e}")
            return None

    async def get_symbol_config(self, strategy_id: str, symbol: str) -> dict | None:
        """
        Get symbol-specific strategy configuration.

        Args:
            strategy_id: Strategy identifier
            symbol: Trading symbol

        Returns:
            Configuration document or None if not found
        """
        if not self._connected:
            return None

        try:
            config = await self.db.strategy_configs_symbol.find_one(
                {"strategy_id": strategy_id, "symbol": symbol}
            )
            return config
        except Exception as e:
            logger.error(
                f"Error fetching symbol config for {strategy_id}/{symbol}: {e}"
            )
            return None

    async def upsert_symbol_config(
        self, strategy_id: str, symbol: str, parameters: dict, metadata: dict
    ) -> str | None:
        """
        Create or update symbol-specific strategy configuration.

        Args:
            strategy_id: Strategy identifier
            symbol: Trading symbol
            parameters: Strategy parameters
            metadata: Additional metadata

        Returns:
            Configuration ID or None on failure
        """
        if not self._connected:
            return None

        try:
            config_doc = {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "parameters": parameters,
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                **metadata,
            }

            result = await self.db.strategy_configs_symbol.replace_one(
                {"strategy_id": strategy_id, "symbol": symbol}, config_doc, upsert=True
            )

            if result.upserted_id:
                logger.info(
                    f"Created symbol config for {strategy_id}/{symbol}: {result.upserted_id}"
                )
                return str(result.upserted_id)
            else:
                logger.info(f"Updated symbol config for {strategy_id}/{symbol}")
                return "updated"

        except Exception as e:
            logger.error(
                f"Error upserting symbol config for {strategy_id}/{symbol}: {e}"
            )
            return None

    async def delete_global_config(self, strategy_id: str) -> bool:
        """
        Delete global strategy configuration.

        Args:
            strategy_id: Strategy identifier

        Returns:
            True if deleted, False otherwise
        """
        if not self._connected:
            return False

        try:
            result = await self.db.strategy_configs_global.delete_one(
                {"strategy_id": strategy_id}
            )
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting global config for {strategy_id}: {e}")
            return False

    async def delete_symbol_config(self, strategy_id: str, symbol: str) -> bool:
        """
        Delete symbol-specific strategy configuration.

        Args:
            strategy_id: Strategy identifier
            symbol: Trading symbol

        Returns:
            True if deleted, False otherwise
        """
        if not self._connected:
            return False

        try:
            result = await self.db.strategy_configs_symbol.delete_one(
                {"strategy_id": strategy_id, "symbol": symbol}
            )
            return result.deleted_count > 0
        except Exception as e:
            logger.error(
                f"Error deleting symbol config for {strategy_id}/{symbol}: {e}"
            )
            return False

    async def list_all_strategy_ids(self) -> list[str]:
        """
        Get list of all strategy IDs with configurations.

        Returns:
            List of unique strategy IDs
        """
        if not self._connected:
            return []

        try:
            global_ids = await self.db.strategy_configs_global.distinct("strategy_id")
            symbol_ids = await self.db.strategy_configs_symbol.distinct("strategy_id")

            # Combine and deduplicate
            all_ids = list(set(global_ids + symbol_ids))
            return sorted(all_ids)

        except Exception as e:
            logger.error(f"Error listing strategy IDs: {e}")
            return []
