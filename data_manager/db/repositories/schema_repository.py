"""
Schema repository for database-specific schema storage and retrieval.
"""

import json
import logging
import uuid
from datetime import datetime

from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.models.schemas import (
    CompatibilityMode,
    SchemaDefinition,
    SchemaRegistration,
    SchemaStatus,
    SchemaUpdate,
    SchemaVersion,
)

logger = logging.getLogger(__name__)


class SchemaRepository:
    """
    Repository for schema operations across MySQL and MongoDB.

    Provides database-agnostic interface for schema management with
    database-specific storage implementations.
    """

    def __init__(self, mysql_adapter: MySQLAdapter, mongodb_adapter: MongoDBAdapter):
        """Initialize schema repository with database adapters."""
        self.mysql_adapter = mysql_adapter
        self.mongodb_adapter = mongodb_adapter

    async def register_schema(
        self, database: str, name: str, registration: SchemaRegistration
    ) -> SchemaDefinition:
        """
        Register a new schema in the specified database.

        Args:
            database: Target database ('mysql' or 'mongodb')
            name: Schema name
            registration: Schema registration data

        Returns:
            Registered schema definition
        """
        schema_id = f"{name}_{registration.version}_{uuid.uuid4().hex[:8]}"

        schema_def = SchemaDefinition(
            name=name,
            version=registration.version,
            schema=registration.schema,
            compatibility_mode=registration.compatibility_mode
            or CompatibilityMode.BACKWARD,
            status=SchemaStatus.ACTIVE,
            description=registration.description,
            created_by=registration.created_by,
        )

        if database == "mysql":
            await self._register_mysql_schema(schema_id, schema_def)
        elif database == "mongodb":
            await self._register_mongodb_schema(schema_id, schema_def)
        else:
            raise ValueError(f"Unsupported database: {database}")

        logger.info(f"Registered schema {name} v{registration.version} in {database}")
        return schema_def

    async def get_schema(
        self, database: str, name: str, version: int | None = None
    ) -> SchemaDefinition | None:
        """
        Get schema by name and optional version.

        Args:
            database: Source database
            name: Schema name
            version: Specific version (None for latest)

        Returns:
            Schema definition or None if not found
        """
        if database == "mysql":
            return await self._get_mysql_schema(name, version)
        elif database == "mongodb":
            return await self._get_mongodb_schema(name, version)
        else:
            raise ValueError(f"Unsupported database: {database}")

    async def list_schemas(
        self,
        database: str | None = None,
        name_pattern: str | None = None,
        status: SchemaStatus | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[SchemaDefinition], int]:
        """
        List schemas with optional filtering.

        Args:
            database: Database filter ('mysql', 'mongodb', or None for both)
            name_pattern: Name pattern filter
            status: Status filter
            page: Page number
            page_size: Page size

        Returns:
            Tuple of (schemas, total_count)
        """
        all_schemas = []
        total_count = 0

        if database is None or database == "mysql":
            mysql_schemas, mysql_count = await self._list_mysql_schemas(
                name_pattern, status, page, page_size
            )
            all_schemas.extend(mysql_schemas)
            total_count += mysql_count

        if database is None or database == "mongodb":
            mongodb_schemas, mongodb_count = await self._list_mongodb_schemas(
                name_pattern, status, page, page_size
            )
            all_schemas.extend(mongodb_schemas)
            total_count += mongodb_count

        # Sort by name and version
        all_schemas.sort(key=lambda x: (x.name, x.version))

        return all_schemas, total_count

    async def get_schema_versions(
        self, database: str, name: str
    ) -> list[SchemaVersion]:
        """
        Get all versions of a schema.

        Args:
            database: Source database
            name: Schema name

        Returns:
            List of schema versions
        """
        if database == "mysql":
            return await self._get_mysql_schema_versions(name)
        elif database == "mongodb":
            return await self._get_mongodb_schema_versions(name)
        else:
            raise ValueError(f"Unsupported database: {database}")

    async def update_schema(
        self, database: str, name: str, version: int, update: SchemaUpdate
    ) -> SchemaDefinition | None:
        """
        Update an existing schema.

        Args:
            database: Target database
            name: Schema name
            version: Schema version
            update: Update data

        Returns:
            Updated schema definition or None if not found
        """
        if database == "mysql":
            return await self._update_mysql_schema(name, version, update)
        elif database == "mongodb":
            return await self._update_mongodb_schema(name, version, update)
        else:
            raise ValueError(f"Unsupported database: {database}")

    async def deprecate_schema(self, database: str, name: str, version: int) -> bool:
        """
        Deprecate a schema version.

        Args:
            database: Target database
            name: Schema name
            version: Schema version

        Returns:
            True if deprecated successfully
        """
        if database == "mysql":
            return await self._deprecate_mysql_schema(name, version)
        elif database == "mongodb":
            return await self._deprecate_mongodb_schema(name, version)
        else:
            raise ValueError(f"Unsupported database: {database}")

    async def search_schemas(
        self, query: str, database: str | None = None
    ) -> list[SchemaDefinition]:
        """
        Search schemas by name or description.

        Args:
            query: Search query
            database: Database filter

        Returns:
            List of matching schemas
        """
        all_schemas = []

        if database is None or database == "mysql":
            mysql_schemas = await self._search_mysql_schemas(query)
            all_schemas.extend(mysql_schemas)

        if database is None or database == "mongodb":
            mongodb_schemas = await self._search_mongodb_schemas(query)
            all_schemas.extend(mongodb_schemas)

        return all_schemas

    # MySQL-specific methods

    async def _register_mysql_schema(
        self, schema_id: str, schema_def: SchemaDefinition
    ) -> None:
        """Register schema in MySQL."""
        try:
            # Convert to dictionary for insertion
            schema_data = {
                "schema_id": schema_id,
                "name": schema_def.name,
                "version": schema_def.version,
                "schema_json": json.dumps(schema_def.schema),
                "compatibility_mode": schema_def.compatibility_mode.value,
                "status": schema_def.status.value,
                "description": schema_def.description,
                "created_at": schema_def.created_at,
                "updated_at": schema_def.updated_at,
                "created_by": schema_def.created_by,
            }

            # Use MySQL adapter to insert
            from pydantic import BaseModel

            class SchemaRecord(BaseModel):
                pass

            # Create a dynamic model for the record
            record = SchemaRecord(**schema_data)
            self.mysql_adapter.write([record], "schemas")

        except Exception as e:
            logger.error(f"Failed to register MySQL schema {schema_def.name}: {e}")
            raise

    async def _get_mysql_schema(
        self, name: str, version: int | None = None
    ) -> SchemaDefinition | None:
        """Get schema from MySQL."""
        try:
            # Query MySQL for schema
            if version:
                # Get specific version
                schemas = self.mysql_adapter.query_range(
                    "schemas", datetime.min, datetime.max, None
                )
                # Filter by name and version
                matching = [
                    s
                    for s in schemas
                    if s.get("name") == name and s.get("version") == version
                ]
            else:
                # Get latest version
                schemas = self.mysql_adapter.query_range(
                    "schemas", datetime.min, datetime.max, None
                )
                # Filter by name and get latest version
                matching = [s for s in schemas if s.get("name") == name]
                if matching:
                    matching = [max(matching, key=lambda x: x.get("version", 0))]

            if not matching:
                return None

            schema_data = matching[0]
            return SchemaDefinition(
                name=schema_data["name"],
                version=schema_data["version"],
                schema=json.loads(schema_data["schema_json"]),
                compatibility_mode=CompatibilityMode(schema_data["compatibility_mode"]),
                status=SchemaStatus(schema_data["status"]),
                description=schema_data.get("description"),
                created_at=schema_data["created_at"],
                updated_at=schema_data["updated_at"],
                created_by=schema_data.get("created_by"),
            )

        except Exception as e:
            logger.error(f"Failed to get MySQL schema {name}: {e}")
            return None

    async def _list_mysql_schemas(
        self,
        name_pattern: str | None,
        status: SchemaStatus | None,
        page: int,
        page_size: int,
    ) -> tuple[list[SchemaDefinition], int]:
        """List schemas from MySQL."""
        try:
            schemas = self.mysql_adapter.query_range(
                "schemas", datetime.min, datetime.max, None
            )

            # Apply filters
            filtered_schemas = []
            for schema_data in schemas:
                if name_pattern and name_pattern not in schema_data.get("name", ""):
                    continue
                if status and SchemaStatus(schema_data.get("status")) != status:
                    continue

                schema_def = SchemaDefinition(
                    name=schema_data["name"],
                    version=schema_data["version"],
                    schema=json.loads(schema_data["schema_json"]),
                    compatibility_mode=CompatibilityMode(
                        schema_data["compatibility_mode"]
                    ),
                    status=SchemaStatus(schema_data["status"]),
                    description=schema_data.get("description"),
                    created_at=schema_data["created_at"],
                    updated_at=schema_data["updated_at"],
                    created_by=schema_data.get("created_by"),
                )
                filtered_schemas.append(schema_def)

            total_count = len(filtered_schemas)

            # Apply pagination
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_schemas = filtered_schemas[start_idx:end_idx]

            return paginated_schemas, total_count

        except Exception as e:
            logger.error(f"Failed to list MySQL schemas: {e}")
            return [], 0

    async def _get_mysql_schema_versions(self, name: str) -> list[SchemaVersion]:
        """Get all versions of a MySQL schema."""
        try:
            schemas = self.mysql_adapter.query_range(
                "schemas", datetime.min, datetime.max, None
            )

            # Filter by name
            matching = [s for s in schemas if s.get("name") == name]

            versions = []
            for schema_data in matching:
                version = SchemaVersion(
                    version=schema_data["version"],
                    schema=json.loads(schema_data["schema_json"]),
                    compatibility_mode=CompatibilityMode(
                        schema_data["compatibility_mode"]
                    ),
                    status=SchemaStatus(schema_data["status"]),
                    description=schema_data.get("description"),
                    created_at=schema_data["created_at"],
                    created_by=schema_data.get("created_by"),
                )
                versions.append(version)

            # Sort by version
            versions.sort(key=lambda x: x.version)
            return versions

        except Exception as e:
            logger.error(f"Failed to get MySQL schema versions for {name}: {e}")
            return []

    async def _update_mysql_schema(
        self, name: str, version: int, update: SchemaUpdate
    ) -> SchemaDefinition | None:
        """Update MySQL schema."""
        # This would require implementing update operations in MySQL adapter
        # For now, return None as update is not implemented
        logger.warning("MySQL schema update not implemented yet")
        return None

    async def _deprecate_mysql_schema(self, name: str, version: int) -> bool:
        """Deprecate MySQL schema."""
        # This would require implementing update operations in MySQL adapter
        # For now, return False as update is not implemented
        logger.warning("MySQL schema deprecation not implemented yet")
        return False

    async def _search_mysql_schemas(self, query: str) -> list[SchemaDefinition]:
        """Search MySQL schemas."""
        try:
            schemas = self.mysql_adapter.query_range(
                "schemas", datetime.min, datetime.max, None
            )

            results = []
            for schema_data in schemas:
                # Simple text search in name and description
                if (
                    query.lower() in schema_data.get("name", "").lower()
                    or query.lower() in schema_data.get("description", "").lower()
                ):
                    schema_def = SchemaDefinition(
                        name=schema_data["name"],
                        version=schema_data["version"],
                        schema=json.loads(schema_data["schema_json"]),
                        compatibility_mode=CompatibilityMode(
                            schema_data["compatibility_mode"]
                        ),
                        status=SchemaStatus(schema_data["status"]),
                        description=schema_data.get("description"),
                        created_at=schema_data["created_at"],
                        updated_at=schema_data["updated_at"],
                        created_by=schema_data.get("created_by"),
                    )
                    results.append(schema_def)

            return results

        except Exception as e:
            logger.error(f"Failed to search MySQL schemas: {e}")
            return []

    # MongoDB-specific methods

    async def _register_mongodb_schema(
        self, schema_id: str, schema_def: SchemaDefinition
    ) -> None:
        """Register schema in MongoDB."""
        try:
            schema_doc = {
                "_id": schema_id,
                "name": schema_def.name,
                "version": schema_def.version,
                "schema": schema_def.schema,
                "compatibility_mode": schema_def.compatibility_mode.value,
                "status": schema_def.status.value,
                "description": schema_def.description,
                "created_at": schema_def.created_at,
                "updated_at": schema_def.updated_at,
                "created_by": schema_def.created_by,
            }

            # Use MongoDB adapter to insert
            from pydantic import BaseModel

            class SchemaRecord(BaseModel):
                pass

            record = SchemaRecord(**schema_doc)
            await self.mongodb_adapter.write([record], "schemas")

        except Exception as e:
            logger.error(f"Failed to register MongoDB schema {schema_def.name}: {e}")
            raise

    async def _get_mongodb_schema(
        self, name: str, version: int | None = None
    ) -> SchemaDefinition | None:
        """Get schema from MongoDB."""
        try:
            if version:
                # Get specific version
                schemas = await self.mongodb_adapter.query_range(
                    "schemas", datetime.min, datetime.max, None
                )
                # Filter by name and version
                matching = [
                    s
                    for s in schemas
                    if s.get("name") == name and s.get("version") == version
                ]
            else:
                # Get latest version
                schemas = await self.mongodb_adapter.query_range(
                    "schemas", datetime.min, datetime.max, None
                )
                # Filter by name and get latest version
                matching = [s for s in schemas if s.get("name") == name]
                if matching:
                    matching = [max(matching, key=lambda x: x.get("version", 0))]

            if not matching:
                return None

            schema_data = matching[0]
            return SchemaDefinition(
                name=schema_data["name"],
                version=schema_data["version"],
                schema=schema_data["schema"],
                compatibility_mode=CompatibilityMode(schema_data["compatibility_mode"]),
                status=SchemaStatus(schema_data["status"]),
                description=schema_data.get("description"),
                created_at=schema_data["created_at"],
                updated_at=schema_data["updated_at"],
                created_by=schema_data.get("created_by"),
            )

        except Exception as e:
            logger.error(f"Failed to get MongoDB schema {name}: {e}")
            return None

    async def _list_mongodb_schemas(
        self,
        name_pattern: str | None,
        status: SchemaStatus | None,
        page: int,
        page_size: int,
    ) -> tuple[list[SchemaDefinition], int]:
        """List schemas from MongoDB."""
        try:
            schemas = await self.mongodb_adapter.query_range(
                "schemas", datetime.min, datetime.max, None
            )

            # Apply filters
            filtered_schemas = []
            for schema_data in schemas:
                if name_pattern and name_pattern not in schema_data.get("name", ""):
                    continue
                if status and SchemaStatus(schema_data.get("status")) != status:
                    continue

                schema_def = SchemaDefinition(
                    name=schema_data["name"],
                    version=schema_data["version"],
                    schema=schema_data["schema"],
                    compatibility_mode=CompatibilityMode(
                        schema_data["compatibility_mode"]
                    ),
                    status=SchemaStatus(schema_data["status"]),
                    description=schema_data.get("description"),
                    created_at=schema_data["created_at"],
                    updated_at=schema_data["updated_at"],
                    created_by=schema_data.get("created_by"),
                )
                filtered_schemas.append(schema_def)

            total_count = len(filtered_schemas)

            # Apply pagination
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_schemas = filtered_schemas[start_idx:end_idx]

            return paginated_schemas, total_count

        except Exception as e:
            logger.error(f"Failed to list MongoDB schemas: {e}")
            return [], 0

    async def _get_mongodb_schema_versions(self, name: str) -> list[SchemaVersion]:
        """Get all versions of a MongoDB schema."""
        try:
            schemas = await self.mongodb_adapter.query_range(
                "schemas", datetime.min, datetime.max, None
            )

            # Filter by name
            matching = [s for s in schemas if s.get("name") == name]

            versions = []
            for schema_data in matching:
                version = SchemaVersion(
                    version=schema_data["version"],
                    schema=schema_data["schema"],
                    compatibility_mode=CompatibilityMode(
                        schema_data["compatibility_mode"]
                    ),
                    status=SchemaStatus(schema_data["status"]),
                    description=schema_data.get("description"),
                    created_at=schema_data["created_at"],
                    created_by=schema_data.get("created_by"),
                )
                versions.append(version)

            # Sort by version
            versions.sort(key=lambda x: x.version)
            return versions

        except Exception as e:
            logger.error(f"Failed to get MongoDB schema versions for {name}: {e}")
            return []

    async def _update_mongodb_schema(
        self, name: str, version: int, update: SchemaUpdate
    ) -> SchemaDefinition | None:
        """Update MongoDB schema."""
        # This would require implementing update operations in MongoDB adapter
        # For now, return None as update is not implemented
        logger.warning("MongoDB schema update not implemented yet")
        return None

    async def _deprecate_mongodb_schema(self, name: str, version: int) -> bool:
        """Deprecate MongoDB schema."""
        # This would require implementing update operations in MongoDB adapter
        # For now, return False as update is not implemented
        logger.warning("MongoDB schema deprecation not implemented yet")
        return False

    async def _search_mongodb_schemas(self, query: str) -> list[SchemaDefinition]:
        """Search MongoDB schemas."""
        try:
            schemas = await self.mongodb_adapter.query_range(
                "schemas", datetime.min, datetime.max, None
            )

            results = []
            for schema_data in schemas:
                # Simple text search in name and description
                if (
                    query.lower() in schema_data.get("name", "").lower()
                    or query.lower() in schema_data.get("description", "").lower()
                ):
                    schema_def = SchemaDefinition(
                        name=schema_data["name"],
                        version=schema_data["version"],
                        schema=schema_data["schema"],
                        compatibility_mode=CompatibilityMode(
                            schema_data["compatibility_mode"]
                        ),
                        status=SchemaStatus(schema_data["status"]),
                        description=schema_data.get("description"),
                        created_at=schema_data["created_at"],
                        updated_at=schema_data["updated_at"],
                        created_by=schema_data.get("created_by"),
                    )
                    results.append(schema_def)

            return results

        except Exception as e:
            logger.error(f"Failed to search MongoDB schemas: {e}")
            return []
