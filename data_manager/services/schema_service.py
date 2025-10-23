"""
Schema service layer for business logic and validation.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import jsonschema
from jsonschema import Draft7Validator, ValidationError

import constants
from data_manager.db.repositories.schema_repository import SchemaRepository
from data_manager.models.schemas import (
    SchemaDefinition,
    SchemaRegistration,
    SchemaUpdate,
    SchemaValidationRequest,
    SchemaValidationResponse,
    SchemaCompatibilityRequest,
    SchemaCompatibilityResponse,
    SchemaStatus,
    CompatibilityMode,
)

logger = logging.getLogger(__name__)


class SchemaService:
    """
    Service layer for schema operations and validation.
    
    Provides business logic for schema management, validation,
    compatibility checking, and version management.
    """

    def __init__(self, schema_repository: SchemaRepository):
        """Initialize schema service with repository."""
        self.repository = schema_repository
        self._schema_cache: Dict[str, SchemaDefinition] = {}
        self._cache_timestamps: Dict[str, float] = {}

    async def register_schema(
        self, 
        database: str, 
        name: str, 
        registration: SchemaRegistration
    ) -> SchemaDefinition:
        """
        Register a new schema with validation and conflict checking.
        
        Args:
            database: Target database
            name: Schema name
            registration: Schema registration data
            
        Returns:
            Registered schema definition
        """
        # Validate schema JSON
        self._validate_schema_json(registration.schema)
        
        # Check for existing schema with same name and version
        existing = await self.repository.get_schema(database, name, registration.version)
        if existing:
            raise ValueError(f"Schema {name} version {registration.version} already exists")
        
        # Check version constraints
        await self._validate_version_constraints(database, name, registration.version)
        
        # Register schema
        schema_def = await self.repository.register_schema(database, name, registration)
        
        # Update cache
        cache_key = f"{database}:{name}:{registration.version}"
        self._schema_cache[cache_key] = schema_def
        self._cache_timestamps[cache_key] = time.time()
        
        logger.info(f"Registered schema {name} v{registration.version} in {database}")
        return schema_def

    async def get_schema(
        self, 
        database: str, 
        name: str, 
        version: Optional[int] = None,
        use_cache: bool = True
    ) -> Optional[SchemaDefinition]:
        """
        Get schema with optional caching.
        
        Args:
            database: Source database
            name: Schema name
            version: Specific version (None for latest)
            use_cache: Whether to use cache
            
        Returns:
            Schema definition or None if not found
        """
        cache_key = f"{database}:{name}:{version or 'latest'}"
        
        # Check cache first
        if use_cache and cache_key in self._schema_cache:
            cache_time = self._cache_timestamps.get(cache_key, 0)
            if time.time() - cache_time < constants.SCHEMA_CACHE_TTL:
                return self._schema_cache[cache_key]
        
        # Get from repository
        schema_def = await self.repository.get_schema(database, name, version)
        
        if schema_def and use_cache:
            self._schema_cache[cache_key] = schema_def
            self._cache_timestamps[cache_key] = time.time()
        
        return schema_def

    async def list_schemas(
        self, 
        database: Optional[str] = None,
        name_pattern: Optional[str] = None,
        status: Optional[SchemaStatus] = None,
        page: int = 1,
        page_size: int = 100
    ) -> Tuple[List[SchemaDefinition], int]:
        """
        List schemas with filtering and pagination.
        
        Args:
            database: Database filter
            name_pattern: Name pattern filter
            status: Status filter
            page: Page number
            page_size: Page size
            
        Returns:
            Tuple of (schemas, total_count)
        """
        return await self.repository.list_schemas(
            database, name_pattern, status, page, page_size
        )

    async def get_schema_versions(self, database: str, name: str) -> List[Dict[str, Any]]:
        """
        Get all versions of a schema.
        
        Args:
            database: Source database
            name: Schema name
            
        Returns:
            List of schema versions
        """
        versions = await self.repository.get_schema_versions(database, name)
        return [
            {
                "version": v.version,
                "schema": v.schema,
                "compatibility_mode": v.compatibility_mode.value,
                "status": v.status.value,
                "description": v.description,
                "created_at": v.created_at.isoformat(),
                "created_by": v.created_by,
            }
            for v in versions
        ]

    async def update_schema(
        self, 
        database: str, 
        name: str, 
        version: int, 
        update: SchemaUpdate
    ) -> Optional[SchemaDefinition]:
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
        # Validate schema JSON if provided
        if update.schema:
            self._validate_schema_json(update.schema)
        
        # Update schema
        updated_schema = await self.repository.update_schema(database, name, version, update)
        
        if updated_schema:
            # Clear cache
            cache_key = f"{database}:{name}:{version}"
            self._schema_cache.pop(cache_key, None)
            self._cache_timestamps.pop(cache_key, None)
        
        return updated_schema

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
        success = await self.repository.deprecate_schema(database, name, version)
        
        if success:
            # Clear cache
            cache_key = f"{database}:{name}:{version}"
            self._schema_cache.pop(cache_key, None)
            self._cache_timestamps.pop(cache_key, None)
        
        return success

    async def validate_data(
        self, 
        request: SchemaValidationRequest
    ) -> SchemaValidationResponse:
        """
        Validate data against a schema.
        
        Args:
            request: Validation request
            
        Returns:
            Validation response
        """
        start_time = time.time()
        
        try:
            # Get schema
            schema_def = await self.get_schema(
                request.database, 
                request.schema_name, 
                request.schema_version
            )
            
            if not schema_def:
                return SchemaValidationResponse(
                    valid=False,
                    errors=[f"Schema {request.schema_name} not found"],
                    schema_used=f"{request.schema_name}:{request.schema_version or 'latest'}",
                    validated_count=0,
                    validation_time_ms=0,
                )
            
            # Prepare data for validation
            data_list = request.data if isinstance(request.data, list) else [request.data]
            
            # Validate each item
            errors = []
            warnings = []
            valid_count = 0
            
            for i, data_item in enumerate(data_list):
                try:
                    # Create validator
                    validator = Draft7Validator(schema_def.schema)
                    
                    # Validate
                    validator.validate(data_item)
                    valid_count += 1
                    
                except ValidationError as e:
                    error_msg = f"Item {i}: {e.message}"
                    if e.path:
                        error_msg += f" (path: {'/'.join(str(p) for p in e.path)})"
                    errors.append(error_msg)
                    
                except Exception as e:
                    errors.append(f"Item {i}: Validation error - {str(e)}")
            
            # Calculate validation time
            validation_time = (time.time() - start_time) * 1000
            
            # Determine overall validity
            is_valid = len(errors) == 0
            
            return SchemaValidationResponse(
                valid=is_valid,
                errors=errors,
                warnings=warnings,
                schema_used=f"{request.schema_name}:{schema_def.version}",
                validated_count=valid_count,
                validation_time_ms=round(validation_time, 2),
            )
            
        except Exception as e:
            logger.error(f"Schema validation error: {e}")
            return SchemaValidationResponse(
                valid=False,
                errors=[f"Validation error: {str(e)}"],
                schema_used=f"{request.schema_name}:{request.schema_version or 'latest'}",
                validated_count=0,
                validation_time_ms=(time.time() - start_time) * 1000,
            )

    async def check_compatibility(
        self, 
        request: SchemaCompatibilityRequest
    ) -> SchemaCompatibilityResponse:
        """
        Check compatibility between two schema versions.
        
        Args:
            request: Compatibility check request
            
        Returns:
            Compatibility response
        """
        try:
            # Get both schemas
            old_schema = await self.get_schema(
                request.database, 
                request.schema_name, 
                request.old_version
            )
            new_schema = await self.get_schema(
                request.database, 
                request.schema_name, 
                request.new_version
            )
            
            if not old_schema or not new_schema:
                return SchemaCompatibilityResponse(
                    compatible=False,
                    compatibility_mode="UNKNOWN",
                    breaking_changes=["One or both schemas not found"],
                    warnings=[],
                    migration_suggestions=[],
                )
            
            # Perform compatibility analysis
            breaking_changes = []
            warnings = []
            migration_suggestions = []
            
            # Check field changes
            old_props = old_schema.schema.get("properties", {})
            new_props = new_schema.schema.get("properties", {})
            
            # Check for removed required fields
            old_required = old_schema.schema.get("required", [])
            new_required = new_schema.schema.get("required", [])
            
            for field in old_required:
                if field not in new_required:
                    breaking_changes.append(f"Required field '{field}' removed")
                elif field not in new_props:
                    breaking_changes.append(f"Required field '{field}' no longer defined")
            
            # Check for new required fields
            for field in new_required:
                if field not in old_required and field in new_props:
                    warnings.append(f"New required field '{field}' added")
                    migration_suggestions.append(f"Ensure all existing data includes field '{field}'")
            
            # Check for type changes
            for field in old_props:
                if field in new_props:
                    old_type = old_props[field].get("type")
                    new_type = new_props[field].get("type")
                    if old_type != new_type:
                        breaking_changes.append(f"Field '{field}' type changed from {old_type} to {new_type}")
            
            # Check for removed fields
            for field in old_props:
                if field not in new_props:
                    warnings.append(f"Field '{field}' removed")
                    migration_suggestions.append(f"Consider data migration for field '{field}'")
            
            # Determine compatibility
            is_compatible = len(breaking_changes) == 0
            
            # Determine compatibility mode
            if is_compatible:
                if old_schema.compatibility_mode == new_schema.compatibility_mode:
                    compatibility_mode = old_schema.compatibility_mode.value
                else:
                    compatibility_mode = "MIXED"
            else:
                compatibility_mode = "INCOMPATIBLE"
            
            return SchemaCompatibilityResponse(
                compatible=is_compatible,
                compatibility_mode=compatibility_mode,
                breaking_changes=breaking_changes,
                warnings=warnings,
                migration_suggestions=migration_suggestions,
            )
            
        except Exception as e:
            logger.error(f"Schema compatibility check error: {e}")
            return SchemaCompatibilityResponse(
                compatible=False,
                compatibility_mode="ERROR",
                breaking_changes=[f"Compatibility check error: {str(e)}"],
                warnings=[],
                migration_suggestions=[],
            )

    async def search_schemas(
        self, 
        query: str, 
        database: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Search schemas by name or description.
        
        Args:
            query: Search query
            database: Database filter
            
        Returns:
            List of matching schemas
        """
        schemas = await self.repository.search_schemas(query, database)
        
        return [
            {
                "name": s.name,
                "version": s.version,
                "database": database or "unknown",
                "status": s.status.value,
                "description": s.description,
                "created_at": s.created_at.isoformat(),
                "created_by": s.created_by,
            }
            for s in schemas
        ]

    def _validate_schema_json(self, schema: Dict[str, Any]) -> None:
        """
        Validate that schema is valid JSON Schema.
        
        Args:
            schema: Schema to validate
            
        Raises:
            ValueError: If schema is invalid
        """
        try:
            # Create a validator to check the schema itself
            Draft7Validator.check_schema(schema)
        except jsonschema.SchemaError as e:
            raise ValueError(f"Invalid JSON Schema: {e.message}")
        except Exception as e:
            raise ValueError(f"Schema validation error: {str(e)}")

    async def _validate_version_constraints(
        self, 
        database: str, 
        name: str, 
        version: int
    ) -> None:
        """
        Validate version constraints.
        
        Args:
            database: Target database
            name: Schema name
            version: Version number
            
        Raises:
            ValueError: If version constraints are violated
        """
        # Check maximum versions limit
        versions = await self.repository.get_schema_versions(database, name)
        if len(versions) >= constants.SCHEMA_MAX_VERSIONS:
            raise ValueError(
                f"Maximum schema versions limit ({constants.SCHEMA_MAX_VERSIONS}) reached for {name}"
            )
        
        # Check version number
        if version <= 0:
            raise ValueError("Schema version must be positive")
        
        # Check for version gaps (warn but don't fail)
        existing_versions = [v.version for v in versions]
        if existing_versions and version not in existing_versions:
            max_existing = max(existing_versions)
            if version < max_existing:
                logger.warning(
                    f"Schema {name} version {version} is less than existing max version {max_existing}"
                )

    def clear_cache(self) -> None:
        """Clear schema cache."""
        self._schema_cache.clear()
        self._cache_timestamps.clear()
        logger.info("Schema cache cleared")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "cache_size": len(self._schema_cache),
            "cached_schemas": list(self._schema_cache.keys()),
            "cache_ttl_seconds": constants.SCHEMA_CACHE_TTL,
        }
