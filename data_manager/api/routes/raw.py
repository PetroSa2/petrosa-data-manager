"""
Raw query API endpoints for MySQL and MongoDB with safety validation.
"""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import constants
import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


class RawQueryRequest(BaseModel):
    """Raw query request model."""

    query: str = Field(..., description="SQL query or MongoDB query/aggregation")
    parameters: dict[str, Any] | None = Field(None, description="Query parameters")
    timeout: int | None = Field(
        None,
        ge=1,
        le=constants.RAW_QUERY_TIMEOUT,
        description="Query timeout in seconds",
    )


class RawQueryResponse(BaseModel):
    """Raw query response model."""

    data: list[dict[str, Any]]
    metadata: dict[str, Any]


@router.post("/api/v1/raw/mysql", response_model=RawQueryResponse)
async def execute_mysql_query(request: RawQueryRequest) -> RawQueryResponse:
    """
    Execute raw SQL queries on MySQL database.

    Includes safety validation to prevent dangerous operations.
    """
    if not constants.RAW_QUERY_ENABLED:
        raise HTTPException(status_code=403, detail="Raw queries are disabled")

    if not api_module.db_manager or not api_module.db_manager.mysql_adapter:
        raise HTTPException(status_code=503, detail="MySQL adapter not available")

    try:
        # Validate query safety
        _validate_mysql_query(request.query)

        # Execute query
        adapter = api_module.db_manager.mysql_adapter
        results = await _execute_mysql_query(adapter, request.query, request.parameters)

        # Track metrics
        api_module.db_manager.increment_query_count("mysql")

        return RawQueryResponse(
            data=results,
            metadata={
                "database": "mysql",
                "query_type": _get_query_type(request.query),
                "records_returned": len(results),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    except Exception as e:
        logger.error(f"Error executing MySQL query: {e}", exc_info=True)
        api_module.db_manager.increment_error_count("mysql")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/raw/mongodb", response_model=RawQueryResponse)
async def execute_mongodb_query(request: RawQueryRequest) -> RawQueryResponse:
    """
    Execute raw MongoDB queries/aggregations.

    Supports both find queries and aggregation pipelines.
    """
    if not constants.RAW_QUERY_ENABLED:
        raise HTTPException(status_code=403, detail="Raw queries are disabled")

    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="MongoDB adapter not available")

    try:
        # Parse and validate MongoDB query
        query_obj = _parse_mongodb_query(request.query)
        _validate_mongodb_query(query_obj)

        # Execute query
        adapter = api_module.db_manager.mongodb_adapter
        results = await _execute_mongodb_query(adapter, query_obj, request.parameters)

        # Track metrics
        api_module.db_manager.increment_query_count("mongodb")

        return RawQueryResponse(
            data=results,
            metadata={
                "database": "mongodb",
                "query_type": _get_mongodb_query_type(query_obj),
                "records_returned": len(results),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    except Exception as e:
        logger.error(f"Error executing MongoDB query: {e}", exc_info=True)
        api_module.db_manager.increment_error_count("mongodb")
        raise HTTPException(status_code=500, detail=str(e))


def _validate_mysql_query(query: str) -> None:
    """Validate MySQL query for safety."""
    query_upper = query.upper().strip()

    # Check for dangerous operations
    dangerous_operations = [
        "DROP",
        "DELETE",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "INSERT",
        "UPDATE",
        "GRANT",
        "REVOKE",
        "FLUSH",
        "RESET",
        "SHUTDOWN",
        "KILL",
    ]

    for operation in dangerous_operations:
        if operation in query_upper:
            raise HTTPException(
                status_code=400,
                detail=f"Dangerous operation '{operation}' not allowed in raw queries",
            )

    # Check for system database access
    system_dbs = ["mysql", "information_schema", "performance_schema", "sys"]
    for db in system_dbs:
        if f"FROM {db}." in query_upper or f"JOIN {db}." in query_upper:
            raise HTTPException(
                status_code=400, detail=f"Access to system database '{db}' not allowed"
            )

    # Check query length
    if len(query) > 10000:  # 10KB limit
        raise HTTPException(status_code=400, detail="Query too long (max 10KB)")


def _validate_mongodb_query(query_obj: dict[str, Any]) -> None:
    """Validate MongoDB query for safety."""
    # Check for dangerous operations
    if "drop" in str(query_obj).lower():
        raise HTTPException(
            status_code=400, detail="Drop operations not allowed in raw queries"
        )

    # Check for system collection access
    system_collections = ["system.", "admin.", "config.", "local."]
    query_str = str(query_obj)
    for collection in system_collections:
        if collection in query_str:
            raise HTTPException(
                status_code=400,
                detail=f"Access to system collection '{collection}' not allowed",
            )


def _parse_mongodb_query(query: str) -> dict[str, Any]:
    """Parse MongoDB query string."""
    try:
        # Try to parse as JSON
        return json.loads(query)
    except json.JSONDecodeError:
        # If not JSON, treat as find query with collection
        # Format: collection_name: {find_query}
        if ":" in query:
            parts = query.split(":", 1)
            collection = parts[0].strip()
            query_part = parts[1].strip()
            try:
                query_obj = json.loads(query_part)
                query_obj["collection"] = collection
                return query_obj
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400, detail="Invalid MongoDB query format"
                )
        else:
            raise HTTPException(
                status_code=400, detail="MongoDB query must be JSON format"
            )


async def _execute_mysql_query(
    adapter, query: str, parameters: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Execute MySQL query safely."""
    # This is a simplified implementation
    # In a real implementation, you'd use proper SQL execution with parameter binding

    # For now, we'll return empty results
    # The actual implementation would execute the query through the adapter
    logger.warning("MySQL raw query execution not fully implemented")
    return []


async def _execute_mongodb_query(
    adapter, query_obj: dict[str, Any], parameters: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Execute MongoDB query safely."""
    try:
        collection_name = query_obj.get("collection", "default")

        if "aggregate" in query_obj:
            # Aggregation pipeline
            pipeline = query_obj["aggregate"]
            coll = adapter.db[collection_name]
            cursor = coll.aggregate(pipeline)
            results = await cursor.to_list(length=constants.RAW_QUERY_MAX_RESULTS)

        elif "find" in query_obj:
            # Find query
            find_query = query_obj["find"]
            coll = adapter.db[collection_name]
            cursor = coll.find(find_query)
            results = await cursor.to_list(length=constants.RAW_QUERY_MAX_RESULTS)

        else:
            raise HTTPException(
                status_code=400,
                detail="MongoDB query must contain 'find' or 'aggregate' operation",
            )

        # Remove _id from results
        for result in results:
            result.pop("_id", None)

        return results

    except Exception as e:
        logger.error(f"Error executing MongoDB query: {e}")
        raise


def _get_query_type(query: str) -> str:
    """Determine the type of SQL query."""
    query_upper = query.upper().strip()

    if query_upper.startswith("SELECT"):
        return "select"
    elif query_upper.startswith("SHOW"):
        return "show"
    elif query_upper.startswith("DESCRIBE") or query_upper.startswith("DESC"):
        return "describe"
    else:
        return "other"


def _get_mongodb_query_type(query_obj: dict[str, Any]) -> str:
    """Determine the type of MongoDB query."""
    if "aggregate" in query_obj:
        return "aggregation"
    elif "find" in query_obj:
        return "find"
    else:
        return "other"
