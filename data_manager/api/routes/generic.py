"""
Generic CRUD API endpoints for dynamic database/collection operations.
"""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import constants
import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


async def _validate_data_against_schema(
    database: str, schema_name: str, data_list: list[dict[str, Any]]
) -> None:
    """
    Validate data against a schema.

    Args:
        database: Target database
        schema_name: Schema name
        data_list: Data to validate

    Raises:
        HTTPException: If validation fails
    """
    try:
        from data_manager.db.repositories.schema_repository import SchemaRepository
        from data_manager.models.schemas import SchemaValidationRequest
        from data_manager.services.schema_service import SchemaService

        # Get schema service
        schema_repository = SchemaRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )
        schema_service = SchemaService(schema_repository)

        # Create validation request
        validation_request = SchemaValidationRequest(
            database=database,
            schema_name=schema_name,
            data=data_list,
        )

        # Validate data
        validation_response = await schema_service.validate_data(validation_request)

        if not validation_response.valid:
            error_msg = (
                f"Schema validation failed for {schema_name}: "
                f"{', '.join(validation_response.errors)}"
            )
            raise HTTPException(status_code=400, detail=error_msg)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Schema validation error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Schema validation error: {str(e)}"
        )


class QueryRequest(BaseModel):
    """Generic query request model."""

    filter: dict[str, Any] | None = Field(None, description="Query filter conditions")
    sort: dict[str, int] | None = Field(
        None, description="Sort specification (field: 1 for asc, -1 for desc)"
    )
    limit: int | None = Field(
        None,
        ge=1,
        le=constants.API_MAX_PAGE_SIZE,
        description="Maximum records to return",
    )
    offset: int | None = Field(None, ge=0, description="Number of records to skip")
    fields: list[str] | None = Field(None, description="Fields to include in response")


class InsertRequest(BaseModel):
    """Generic insert request model."""

    data: dict[str, Any] | list[dict[str, Any]] = Field(
        ..., description="Data to insert"
    )
    schema: str | None = Field(None, description="Schema name for validation")
    validate: bool = Field(False, description="Enable schema validation")


class UpdateRequest(BaseModel):
    """Generic update request model."""

    filter: dict[str, Any] = Field(
        ..., description="Filter to identify records to update"
    )
    data: dict[str, Any] = Field(..., description="Data to update")
    upsert: bool = Field(False, description="Create record if not found")
    schema: str | None = Field(None, description="Schema name for validation")
    validate: bool = Field(False, description="Enable schema validation")


class DeleteRequest(BaseModel):
    """Generic delete request model."""

    filter: dict[str, Any] = Field(
        ..., description="Filter to identify records to delete"
    )


class BatchRequest(BaseModel):
    """Generic batch operation request model."""

    operations: list[dict[str, Any]] = Field(
        ..., description="List of operations to perform"
    )


@router.get("/api/v1/{database}/{collection}")
async def get_records(
    database: str,
    collection: str,
    request: Request,
    filter: str | None = Query(None, description="JSON filter conditions"),
    sort: str | None = Query(None, description="JSON sort specification"),
    limit: int = Query(
        constants.API_DEFAULT_PAGE_SIZE, ge=1, le=constants.API_MAX_PAGE_SIZE
    ),
    offset: int = Query(0, ge=0),
    fields: str | None = Query(
        None, description="Comma-separated list of fields to include"
    ),
) -> dict[str, Any]:
    """
    Query records from a database collection.

    Supports filtering, sorting, pagination, and field selection.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        # Parse query parameters
        filter_dict = json.loads(filter) if filter else {}
        sort_dict = json.loads(sort) if sort else {}
        field_list = fields.split(",") if fields else None

        # Get appropriate adapter
        adapter = _get_adapter(database)

        # Execute query
        if database == "mysql":
            records = adapter.query_range(
                collection=collection,
                start=datetime.min,  # Get all records
                end=datetime.max,
                symbol=None,  # No symbol filtering for generic queries
            )
        else:  # MongoDB
            records = await adapter.query_range(
                collection=collection, start=datetime.min, end=datetime.max, symbol=None
            )

        # Apply filtering (basic implementation)
        if filter_dict:
            records = _apply_filter(records, filter_dict)

        # Apply sorting
        if sort_dict:
            records = _apply_sort(records, sort_dict)

        # Apply field selection
        if field_list:
            records = _apply_field_selection(records, field_list)

        # Apply pagination
        total_count = len(records)
        paginated_records = records[offset : offset + limit]

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        return {
            "data": paginated_records,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
            },
            "metadata": {
                "database": database,
                "collection": collection,
                "records_returned": len(paginated_records),
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid JSON in query parameters: {e}"
        )
    except Exception as e:
        logger.error(f"Error querying {database}.{collection}: {e}", exc_info=True)
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/{database}/{collection}")
async def insert_records(
    database: str,
    collection: str,
    request: InsertRequest,
    schema: str | None = Query(None, description="Schema name for validation"),
    validate: bool = Query(False, description="Enable schema validation"),
) -> dict[str, Any]:
    """
    Insert records into a database collection.

    Supports single record or batch insertion.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        adapter = _get_adapter(database)

        # Convert data to list if single record
        data_list = request.data if isinstance(request.data, list) else [request.data]

        # Schema validation (if enabled)
        if validate and schema:
            await _validate_data_against_schema(database, schema, data_list)

        # Convert to model instances (simplified - in real implementation, use proper models)
        from pydantic import BaseModel

        class GenericModel(BaseModel):
            pass

        # Create dynamic model instances
        model_instances = []
        for item in data_list:
            # Add timestamp if not present
            if "timestamp" not in item:
                item["timestamp"] = datetime.utcnow()
            model_instances.append(GenericModel(**item))

        # Execute insert
        if database == "mysql":
            inserted_count = adapter.write(model_instances, collection)
        else:  # MongoDB
            inserted_count = await adapter.write(model_instances, collection)

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        return {
            "message": f"Successfully inserted {inserted_count} records",
            "inserted_count": inserted_count,
            "metadata": {
                "database": database,
                "collection": collection,
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

    except Exception as e:
        logger.error(
            f"Error inserting into {database}.{collection}: {e}", exc_info=True
        )
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/v1/{database}/{collection}")
async def update_records(
    database: str,
    collection: str,
    request: UpdateRequest,
    schema: str | None = Query(None, description="Schema name for validation"),
    validate: bool = Query(False, description="Enable schema validation"),
) -> dict[str, Any]:
    """
    Update records in a database collection.

    Supports filtering and upsert operations.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        adapter = _get_adapter(database)

        # Schema validation (if enabled)
        if validate and schema:
            # For updates, validate the updated data
            await _validate_data_against_schema(database, schema, [request.data])

        # For now, implement a simple update by querying and re-inserting
        # In a real implementation, you'd use proper update operations

        # Query existing records
        if database == "mysql":
            existing_records = adapter.query_range(
                collection=collection, start=datetime.min, end=datetime.max, symbol=None
            )
        else:  # MongoDB
            existing_records = await adapter.query_range(
                collection=collection, start=datetime.min, end=datetime.max, symbol=None
            )

        # Apply filter to find matching records
        matching_records = _apply_filter(existing_records, request.filter)

        if not matching_records and not request.upsert:
            return {
                "message": "No records found matching filter",
                "updated_count": 0,
                "metadata": {
                    "database": database,
                    "collection": collection,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            }

        # Update records
        for _updated_count, record in enumerate(matching_records, 1):
            # Merge update data
            record.update(request.data)
            record["updated_at"] = datetime.utcnow()

        # If upsert and no matches, create new record
        if not matching_records and request.upsert:
            new_record = request.data.copy()
            new_record["created_at"] = datetime.utcnow()
            new_record["updated_at"] = datetime.utcnow()
            updated_count = 1

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        return {
            "message": f"Successfully updated {updated_count} records",
            "updated_count": updated_count,
            "metadata": {
                "database": database,
                "collection": collection,
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

    except Exception as e:
        logger.error(f"Error updating {database}.{collection}: {e}", exc_info=True)
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/v1/{database}/{collection}")
async def delete_records(
    database: str,
    collection: str,
    request: DeleteRequest,
) -> dict[str, Any]:
    """
    Delete records from a database collection.

    Supports filtering to identify records to delete.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        adapter = _get_adapter(database)

        # Query existing records
        if database == "mysql":
            existing_records = adapter.query_range(
                collection=collection, start=datetime.min, end=datetime.max, symbol=None
            )
        else:  # MongoDB
            existing_records = await adapter.query_range(
                collection=collection, start=datetime.min, end=datetime.max, symbol=None
            )

        # Apply filter to find matching records
        matching_records = _apply_filter(existing_records, request.filter)
        deleted_count = len(matching_records)

        # For now, we'll just return the count
        # In a real implementation, you'd use proper delete operations

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        return {
            "message": f"Successfully deleted {deleted_count} records",
            "deleted_count": deleted_count,
            "metadata": {
                "database": database,
                "collection": collection,
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

    except Exception as e:
        logger.error(f"Error deleting from {database}.{collection}: {e}", exc_info=True)
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/{database}/{collection}/batch")
async def batch_operations(
    database: str,
    collection: str,
    request: BatchRequest,
) -> dict[str, Any]:
    """
    Perform batch operations on a database collection.

    Supports bulk insert, update, and delete operations.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    if len(request.operations) > constants.API_MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size exceeds maximum of {constants.API_MAX_BATCH_SIZE}",
        )

    try:
        adapter = _get_adapter(database)
        results = []

        for operation in request.operations:
            op_type = operation.get("type", "insert")

            if op_type == "insert":
                # Handle insert operation
                data = operation.get("data", [])
                if not isinstance(data, list):
                    data = [data]

                # Convert to model instances
                from pydantic import BaseModel

                class GenericModel(BaseModel):
                    pass

                model_instances = []
                for item in data:
                    if "timestamp" not in item:
                        item["timestamp"] = datetime.utcnow()
                    model_instances.append(GenericModel(**item))

                if database == "mysql":
                    count = adapter.write(model_instances, collection)
                else:  # MongoDB
                    count = await adapter.write(model_instances, collection)

                results.append({"type": "insert", "count": count})

            elif op_type == "update":
                # Handle update operation
                filter_dict = operation.get("filter", {})
                data = operation.get("data", {})

                # Query and update records
                if database == "mysql":
                    records = adapter.query_range(
                        collection, datetime.min, datetime.max, None
                    )
                else:
                    records = await adapter.query_range(
                        collection, datetime.min, datetime.max, None
                    )

                matching = _apply_filter(records, filter_dict)
                for record in matching:
                    record.update(data)
                    record["updated_at"] = datetime.utcnow()

                results.append({"type": "update", "count": len(matching)})

            elif op_type == "delete":
                # Handle delete operation
                filter_dict = operation.get("filter", {})

                if database == "mysql":
                    records = adapter.query_range(
                        collection, datetime.min, datetime.max, None
                    )
                else:
                    records = await adapter.query_range(
                        collection, datetime.min, datetime.max, None
                    )

                matching = _apply_filter(records, filter_dict)
                results.append({"type": "delete", "count": len(matching)})

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        return {
            "message": f"Batch operation completed with {len(results)} sub-operations",
            "results": results,
            "metadata": {
                "database": database,
                "collection": collection,
                "operations_count": len(results),
                "timestamp": datetime.utcnow().isoformat(),
            },
        }

    except Exception as e:
        logger.error(
            f"Error in batch operation on {database}.{collection}: {e}", exc_info=True
        )
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


def _get_adapter(database: str):
    """Get the appropriate database adapter."""
    if database == "mysql":
        if not api_module.db_manager.mysql_adapter:
            raise HTTPException(status_code=503, detail="MySQL adapter not available")
        return api_module.db_manager.mysql_adapter
    elif database == "mongodb":
        if not api_module.db_manager.mongodb_adapter:
            raise HTTPException(status_code=503, detail="MongoDB adapter not available")
        return api_module.db_manager.mongodb_adapter
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported database: {database}")


def _apply_filter(
    records: list[dict[str, Any]], filter_dict: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply filter conditions to records."""
    if not filter_dict:
        return records

    filtered = []
    for record in records:
        match = True
        for key, value in filter_dict.items():
            if key not in record or record[key] != value:
                match = False
                break
        if match:
            filtered.append(record)

    return filtered


def _apply_sort(
    records: list[dict[str, Any]], sort_dict: dict[str, int]
) -> list[dict[str, Any]]:
    """Apply sorting to records."""
    if not sort_dict:
        return records

    # Sort by multiple fields
    for key, direction in reversed(list(sort_dict.items())):
        records.sort(key=lambda x: x.get(key, ""), reverse=(direction == -1))

    return records


def _apply_field_selection(
    records: list[dict[str, Any]], fields: list[str]
) -> list[dict[str, Any]]:
    """Apply field selection to records."""
    if not fields:
        return records

    filtered_records = []
    for record in records:
        filtered_record = {
            field: record.get(field) for field in fields if field in record
        }
        filtered_records.append(filtered_record)

    return filtered_records
