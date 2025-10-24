"""
Schema registry REST API endpoints.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.db.repositories.schema_repository import SchemaRepository
from data_manager.models.schemas import (
    SchemaBootstrapRequest,
    SchemaBootstrapResponse,
    SchemaCompatibilityRequest,
    SchemaCompatibilityResponse,
    SchemaListResponse,
    SchemaRegistration,
    SchemaStatus,
    SchemaUpdate,
    SchemaValidationRequest,
    SchemaValidationResponse,
    SchemaVersionListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Global schema service reference (will be set by main app)
schema_service = None


def get_schema_service():
    """Get schema service instance."""
    global schema_service
    if schema_service is None:
        if not api_module.db_manager:
            raise HTTPException(
                status_code=503, detail="Database manager not available"
            )

        schema_repository = SchemaRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )
        from data_manager.services.schema_service import SchemaService

        schema_service = SchemaService(schema_repository)

    return schema_service


@router.post("/schemas/{database}/{name}")
async def register_schema(
    database: str,
    name: str,
    registration: SchemaRegistration,
) -> dict[str, Any]:
    """
    Register a new schema in the specified database.

    Args:
        database: Target database ('mysql' or 'mongodb')
        name: Schema name
        registration: Schema registration data

    Returns:
        Registered schema information
    """
    if database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        schema_def = await service.register_schema(database, name, registration)

        return {
            "message": f"Schema {name} v{registration.version} registered successfully",
            "schema": {
                "name": schema_def.name,
                "version": schema_def.version,
                "database": database,
                "status": schema_def.status.value,
                "compatibility_mode": schema_def.compatibility_mode.value,
                "description": schema_def.description,
                "created_at": schema_def.created_at.isoformat(),
                "created_by": schema_def.created_by,
            },
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error registering schema {name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/{database}/{name}")
async def get_schema(
    database: str,
    name: str,
    version: int | None = Query(None, description="Specific version (None for latest)"),
) -> dict[str, Any]:
    """
    Get schema by name and optional version.

    Args:
        database: Source database
        name: Schema name
        version: Specific version (None for latest)

    Returns:
        Schema definition
    """
    if database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        schema_def = await service.get_schema(database, name, version)

        if not schema_def:
            raise HTTPException(
                status_code=404, detail=f"Schema {name} not found in {database}"
            )

        return {
            "name": schema_def.name,
            "version": schema_def.version,
            "database": database,
            "schema": schema_def.schema,
            "compatibility_mode": schema_def.compatibility_mode.value,
            "status": schema_def.status.value,
            "description": schema_def.description,
            "created_at": schema_def.created_at.isoformat(),
            "updated_at": schema_def.updated_at.isoformat(),
            "created_by": schema_def.created_by,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting schema {name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/{database}/{name}/versions")
async def get_schema_versions(
    database: str,
    name: str,
) -> SchemaVersionListResponse:
    """
    Get all versions of a schema.

    Args:
        database: Source database
        name: Schema name

    Returns:
        List of schema versions
    """
    if database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        versions = await service.get_schema_versions(database, name)

        if not versions:
            raise HTTPException(
                status_code=404,
                detail=f"No versions found for schema {name} in {database}",
            )

        latest_version = max(v["version"] for v in versions)

        return SchemaVersionListResponse(
            schema_name=name,
            versions=versions,
            total_versions=len(versions),
            latest_version=latest_version,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting schema versions for {name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/{database}/{name}/versions/{version}")
async def get_schema_version(
    database: str,
    name: str,
    version: int,
) -> dict[str, Any]:
    """
    Get specific version of a schema.

    Args:
        database: Source database
        name: Schema name
        version: Schema version

    Returns:
        Schema version definition
    """
    if database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        schema_def = await service.get_schema(database, name, version)

        if not schema_def:
            raise HTTPException(
                status_code=404,
                detail=f"Schema {name} version {version} not found in {database}",
            )

        return {
            "name": schema_def.name,
            "version": schema_def.version,
            "database": database,
            "schema": schema_def.schema,
            "compatibility_mode": schema_def.compatibility_mode.value,
            "status": schema_def.status.value,
            "description": schema_def.description,
            "created_at": schema_def.created_at.isoformat(),
            "updated_at": schema_def.updated_at.isoformat(),
            "created_by": schema_def.created_by,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting schema {name} v{version}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/schemas/{database}/{name}/versions/{version}")
async def update_schema(
    database: str,
    name: str,
    version: int,
    update: SchemaUpdate,
) -> dict[str, Any]:
    """
    Update an existing schema.

    Args:
        database: Target database
        name: Schema name
        version: Schema version
        update: Update data

    Returns:
        Updated schema information
    """
    if database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        updated_schema = await service.update_schema(database, name, version, update)

        if not updated_schema:
            raise HTTPException(
                status_code=404,
                detail=f"Schema {name} version {version} not found in {database}",
            )

        return {
            "message": f"Schema {name} v{version} updated successfully",
            "schema": {
                "name": updated_schema.name,
                "version": updated_schema.version,
                "database": database,
                "status": updated_schema.status.value,
                "compatibility_mode": updated_schema.compatibility_mode.value,
                "description": updated_schema.description,
                "updated_at": updated_schema.updated_at.isoformat(),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating schema {name} v{version}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/schemas/{database}/{name}/versions/{version}")
async def deprecate_schema(
    database: str,
    name: str,
    version: int,
) -> dict[str, Any]:
    """
    Deprecate a schema version.

    Args:
        database: Target database
        name: Schema name
        version: Schema version

    Returns:
        Deprecation result
    """
    if database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        success = await service.deprecate_schema(database, name, version)

        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Schema {name} version {version} not found in {database}",
            )

        return {
            "message": f"Schema {name} v{version} deprecated successfully",
            "deprecated": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deprecating schema {name} v{version}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas")
async def list_schemas(
    database: str | None = Query(
        None, description="Database filter ('mysql', 'mongodb', or None for both)"
    ),
    name_pattern: str | None = Query(None, description="Name pattern filter"),
    status: SchemaStatus | None = Query(None, description="Status filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(100, ge=1, le=1000, description="Page size"),
) -> SchemaListResponse:
    """
    List schemas with optional filtering.

    Args:
        database: Database filter
        name_pattern: Name pattern filter
        status: Status filter
        page: Page number
        page_size: Page size

    Returns:
        List of schemas with pagination info
    """
    if database and database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        schemas, total_count = await service.list_schemas(
            database, name_pattern, status, page, page_size
        )

        schema_list = [
            {
                "name": s.name,
                "version": s.version,
                "database": database or "unknown",
                "status": s.status.value,
                "compatibility_mode": s.compatibility_mode.value,
                "description": s.description,
                "created_at": s.created_at.isoformat(),
                "created_by": s.created_by,
            }
            for s in schemas
        ]

        return SchemaListResponse(
            schemas=schema_list,
            total_count=total_count,
            database=database,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total_count,
        )

    except Exception as e:
        logger.error(f"Error listing schemas: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/schemas/validate")
async def validate_data(
    request: SchemaValidationRequest,
) -> SchemaValidationResponse:
    """
    Validate data against a schema.

    Args:
        request: Validation request

    Returns:
        Validation response
    """
    try:
        service = get_schema_service()
        return await service.validate_data(request)

    except Exception as e:
        logger.error(f"Error validating data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/schemas/compatibility")
async def check_compatibility(
    request: SchemaCompatibilityRequest,
) -> SchemaCompatibilityResponse:
    """
    Check compatibility between two schema versions.

    Args:
        request: Compatibility check request

    Returns:
        Compatibility response
    """
    try:
        service = get_schema_service()
        return await service.check_compatibility(request)

    except Exception as e:
        logger.error(f"Error checking compatibility: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/search")
async def search_schemas(
    query: str = Query(..., description="Search query"),
    database: str | None = Query(None, description="Database filter"),
) -> list[dict[str, Any]]:
    """
    Search schemas by name or description.

    Args:
        query: Search query
        database: Database filter

    Returns:
        List of matching schemas
    """
    if database and database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        return await service.search_schemas(query, database)

    except Exception as e:
        logger.error(f"Error searching schemas: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/schemas/bootstrap")
async def bootstrap_schemas(
    request: SchemaBootstrapRequest,
) -> SchemaBootstrapResponse:
    """
    Bootstrap common schemas for a database.

    Args:
        request: Bootstrap request

    Returns:
        Bootstrap response
    """
    if request.database not in ["mysql", "mongodb"]:
        raise HTTPException(
            status_code=400, detail="Database must be 'mysql' or 'mongodb'"
        )

    try:
        service = get_schema_service()
        registered_count = 0
        skipped_count = 0
        errors = []
        registered_schemas = []

        for schema_reg in request.schemas:
            try:
                # Check if schema already exists
                existing = await service.get_schema(
                    request.database, schema_reg.name, schema_reg.version
                )

                if existing and not request.overwrite_existing:
                    skipped_count += 1
                    continue

                # Register schema
                await service.register_schema(
                    request.database, schema_reg.name, schema_reg
                )
                registered_count += 1
                registered_schemas.append(schema_reg.name)

            except Exception as e:
                error_msg = f"Failed to register {schema_reg.name}: {str(e)}"
                errors.append(error_msg)
                logger.error(error_msg)

        return SchemaBootstrapResponse(
            success=len(errors) == 0,
            registered_count=registered_count,
            skipped_count=skipped_count,
            errors=errors,
            registered_schemas=registered_schemas,
        )

    except Exception as e:
        logger.error(f"Error bootstrapping schemas: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas/cache/stats")
async def get_cache_stats() -> dict[str, Any]:
    """
    Get schema cache statistics.

    Returns:
        Cache statistics
    """
    try:
        service = get_schema_service()
        return service.get_cache_stats()

    except Exception as e:
        logger.error(f"Error getting cache stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/schemas/cache/clear")
async def clear_cache() -> dict[str, Any]:
    """
    Clear schema cache.

    Returns:
        Cache clear result
    """
    try:
        service = get_schema_service()
        service.clear_cache()

        return {
            "message": "Schema cache cleared successfully",
            "cleared": True,
        }

    except Exception as e:
        logger.error(f"Error clearing cache: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
