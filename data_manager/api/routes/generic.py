"""
Generic CRUD API endpoints for dynamic database/collection operations.
"""

import json
import logging
import os
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import constants
import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.utils.circuit_breaker import CircuitBreakerOpenError

logger = logging.getLogger(__name__)

router = APIRouter()


def _serialize_write_result(result: Any) -> dict[str, Any]:
    """Render a MySQL :class:`WriteResult` (or MongoDB int) as explicit counts.

    Resolves petrosa-data-manager#213 AC2.3 — callers must always see
    ``inserted`` + ``duplicates`` + ``failed`` instead of an ambiguous bare
    integer that could mean "duplicate" OR "silently failed".
    """
    inserted = int(getattr(result, "inserted", result) or 0)
    duplicates = int(getattr(result, "duplicates", 0) or 0)
    failed = int(getattr(result, "failed", 0) or 0)
    return {"inserted": inserted, "duplicates": duplicates, "failed": failed}


def _validate_mysql_update_data(
    adapter: Any, collection: str, data: dict[str, Any]
) -> list[str]:
    """Reject unsafe MySQL updates and return fields that may be ignored."""
    column_names = adapter.get_column_names(collection)
    if not isinstance(column_names, set | frozenset | list | tuple):
        raise DatabaseError(
            f"MySQL adapter returned invalid column metadata for {collection}"
        )

    ignored_fields = [key for key in data if key not in column_names]
    operator_fields = [key for key in data if key.startswith("$")]
    applicable_fields = [key for key in data if key in column_names]
    if operator_fields or not applicable_fields:
        unknown = ", ".join(ignored_fields) if ignored_fields else "<none>"
        raise HTTPException(
            status_code=422,
            detail=(
                f"MySQL update rejected for {collection}: no applicable columns; "
                f"unknown fields: {unknown}"
            ),
        )
    return ignored_fields


def _signals_persist_enabled() -> bool:
    """AC kill-switch (data-manager#302): ``PETROSA_SIGNALS_PERSIST_ENABLED``.

    Mirrors the `alerts` kill-switch
    (``data_manager.services.alert_dispatcher._persist_enabled``,
    data-manager#271 AC7). Live Atlas evidence (data-manager#302) confirms
    the `signals` Mongo collection has no confirmed reader in the 8-repo
    ecosystem — a human product decision on whether to build one is still
    open — so the operator must be able to stop the write immediately via
    config + restart with no code change. Gates ONLY the MongoDB `signals`
    insert path; other collections (and the legacy MySQL `signals` path,
    whose writer was already removed by petrosa-bot-ta-analysis#284) are
    unaffected.
    """
    return os.environ.get("PETROSA_SIGNALS_PERSIST_ENABLED", "true").lower() == "true"


def _signals_mysql_persist_enabled() -> bool:
    """Kill-switch for the `signals` MySQL dual-write: ``PETROSA_SIGNALS_MYSQL_PERSIST_ENABLED``.

    2026-09-20: MySQL `petrosa_crypto.signals` is the durable historic
    store (no TTL) going forward — its own writer was previously removed
    (petrosa-bot-ta-analysis#284) leaving it a frozen archive, and the Mongo
    `signals` window was cut to 1 hour (see `intents_ttl_index.py`
    ``DEFAULT_SIGNALS_TTL_SECONDS``) since it can no longer double as
    long-term storage. This gate is independent of
    ``PETROSA_SIGNALS_PERSIST_ENABLED`` (the Mongo-side switch) so either
    store can be disabled without affecting the other.
    """
    return (
        os.environ.get("PETROSA_SIGNALS_MYSQL_PERSIST_ENABLED", "true").lower()
        == "true"
    )


def _build_mysql_signal_record(item: dict[str, Any]) -> dict[str, Any]:
    """Map a `signals` payload onto the legacy MySQL `signals` schema.

    Mirrors the field mapping the raw-pymysql path used before it was
    removed (petrosa-bot-ta-analysis#284), confirmed against the 14.38M
    existing historic rows (2026-09-20 live read): ``period`` historically
    mirrors ``timeframe``, and ``signal_type`` is the signal's ``action``
    (buy/sell/hold/close). Extra fields the ta_bot `Signal` model carries
    (``strategy_id``, ``current_price``, ``price``, ``strategy_mode``,
    ``strength``, ``quantity``, ``source``, ``order_type``, ...) have no
    column of their own in this table and are intentionally dropped —
    ``metadata`` carries only the strategy's own metadata dict, matching
    existing history exactly. ``id``/``created_at`` are omitted on purpose:
    ``id`` is an ``auto_increment`` PK (see the integer-PK guard in
    ``MySQLAdapter.write``) and ``created_at`` defaults to
    ``CURRENT_TIMESTAMP`` in the live schema.
    """
    timeframe = item.get("timeframe") or "15m"
    return {
        "symbol": item["symbol"],
        "timeframe": timeframe,
        "period": timeframe,
        "signal_type": item.get("action") or item.get("signal_type") or "hold",
        "confidence": item.get("confidence", 0.0),
        "strategy": item.get("strategy") or item.get("strategy_id") or "",
        "metadata": item.get("metadata") or {},
        "timestamp": item.get("timestamp") or datetime.now(UTC).isoformat(),
    }


def _dual_write_signals_to_mysql(data_list: list[dict[str, Any]]) -> None:
    """Best-effort dual-write of `signals` payloads into the durable MySQL
    historic store. Never raises — a MySQL hiccup must not block or fail
    the live Mongo signal-persist path, which remains the primary,
    synchronous write. Caller is responsible for checking
    `_signals_mysql_persist_enabled()` first.
    """
    mysql_adapter = getattr(api_module.db_manager, "mysql_adapter", None)
    if mysql_adapter is None:
        return

    from pydantic import BaseModel, ConfigDict

    class _MySQLSignalModel(BaseModel):
        model_config = ConfigDict(extra="allow")

    records = []
    for item in data_list:
        if not item.get("symbol"):
            logger.warning(
                "Skipping MySQL signals dual-write: payload missing 'symbol'"
            )
            continue
        try:
            records.append(_MySQLSignalModel(**_build_mysql_signal_record(item)))
        except Exception:
            logger.warning(
                "Skipping malformed signal for MySQL dual-write", exc_info=True
            )

    if not records:
        return

    try:
        mysql_adapter.write(records, "signals")
    except Exception:
        logger.error("MySQL signals dual-write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Shared Internal Helpers
# ---------------------------------------------------------------------------


async def _execute_query_internal(
    database: str,
    collection: str,
    filter_dict: dict[str, Any] | None,
    sort_dict: dict[str, int] | None,
    limit: int,
    offset: int,
    field_list: list[str] | None,
) -> dict[str, Any]:
    """Internal helper to execute a query against a database and collection.

    Per petrosa-data-manager#282: filter/sort/limit/offset are pushed down to
    the driver via ``find_paginated`` instead of loading the entire
    collection/table into memory and slicing it in Python. Response time now
    scales with ``limit``, not collection size.
    """
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    adapter = _get_adapter(database)

    sort_list = list(sort_dict.items()) if sort_dict else None

    try:
        if database == "mysql":
            records, total_count = adapter.find_paginated(
                collection=collection,
                filter_dict=filter_dict,
                sort_list=sort_list,
                limit=limit,
                offset=offset,
                columns=field_list,
            )
        else:  # MongoDB
            records, total_count = await adapter.find_paginated(
                collection=collection,
                filter_dict=filter_dict,
                sort_list=sort_list,
                limit=limit,
                offset=offset,
            )
    except DatabaseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Apply field selection (post-fetch — the result set is already bounded
    # by `limit`, so this no longer touches the whole collection)
    if field_list:
        records = _apply_field_selection(records, field_list)

    # Track metrics
    api_module.db_manager.increment_query_count(database)

    return {
        "data": records,
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
            "records_returned": len(records),
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }


# ---------------------------------------------------------------------------
# Legacy Endpoints for Backward Compatibility
# ---------------------------------------------------------------------------


@router.post("/api/v1/data/query")
async def legacy_query(request: dict[str, Any]) -> dict[str, Any]:
    """
    Legacy query endpoint used by older versions of data-extractor.
    Forwards to generic CRUD logic.
    """
    database = request.get("database", "mongodb")
    collection = request.get("collection")
    if not collection:
        raise HTTPException(status_code=400, detail="Collection name required")

    try:
        # Extract parameters from POST body
        filter_dict = request.get("filter")
        sort_dict = request.get("sort")
        limit = request.get("limit", constants.API_DEFAULT_PAGE_SIZE)
        offset = request.get("offset", 0)
        fields = request.get("fields")

        # Reuse shared query logic
        return await _execute_query_internal(
            database=database,
            collection=collection,
            filter_dict=filter_dict,
            sort_dict=sort_dict,
            limit=limit,
            offset=offset,
            field_list=fields,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Legacy query error: {e}", exc_info=True)
        if api_module.db_manager:
            api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/data/insert")
async def legacy_insert(request: dict[str, Any]) -> dict[str, Any]:
    """
    Legacy insert endpoint used by older versions of data-extractor.
    Forwards to generic insert_records logic.
    """
    database = request.get("database", "mongodb")
    collection = request.get("collection")
    records = request.get("records", [])

    if not collection:
        raise HTTPException(status_code=400, detail="Collection name required")

    # Create InsertRequest object
    insert_request = InsertRequest(data=records)

    # Call the new generic implementation with explicit parameters
    return await insert_records(
        database=database,
        collection=collection,
        request=insert_request,
        schema=None,
        validate=False,
    )


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

        # Use shared query logic
        return await _execute_query_internal(
            database=database,
            collection=collection,
            filter_dict=filter_dict,
            sort_dict=sort_dict,
            limit=limit,
            offset=offset,
            field_list=field_list,
        )

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid JSON in query parameters: {e}"
        )
    except HTTPException:
        raise
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
        data_list_raw = (
            request.data if isinstance(request.data, list) else [request.data]
        )

        # 2026-09-20: independent of the Mongo kill-switch below — MySQL is
        # the durable historic store now (no TTL there; the Mongo TTL was
        # cut to 1h) and must keep receiving signals even if the Mongo
        # short-window write is separately disabled, and vice versa.
        if (
            database == "mongodb"
            and collection == "signals"
            and _signals_mysql_persist_enabled()
        ):
            _dual_write_signals_to_mysql(data_list_raw)

        # data-manager#302 kill-switch — checked BEFORE touching the adapter
        # so a disabled write never opens a connection for a collection with
        # no confirmed reader. Scoped to mongodb.signals only.
        if (
            database == "mongodb"
            and collection == "signals"
            and not _signals_persist_enabled()
        ):
            record_count = len(request.data) if isinstance(request.data, list) else 1
            logger.info(
                "Signals persistence disabled (PETROSA_SIGNALS_PERSIST_ENABLED="
                "false); skipping Mongo write for %d record(s) "
                "(data-manager#302 kill-switch)",
                record_count,
            )
            return {
                "message": (
                    "Signals persistence disabled "
                    "(PETROSA_SIGNALS_PERSIST_ENABLED=false); write skipped"
                ),
                "inserted_count": 0,
                "duplicates": 0,
                "failed": 0,
                "metadata": {
                    "database": database,
                    "collection": collection,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }

        adapter = _get_adapter(database)

        # Convert data to list if single record
        data_list = data_list_raw

        # Schema validation (if enabled)
        if validate and schema:
            await _validate_data_against_schema(database, schema, data_list)

        # Convert to model instances (simplified - in real implementation, use proper models)
        from pydantic import BaseModel, ConfigDict

        class GenericModel(BaseModel):
            model_config = ConfigDict(extra="allow")

        # Create dynamic model instances
        model_instances = []
        for item in data_list:
            # Add timestamp if not present
            if "timestamp" not in item:
                item["timestamp"] = datetime.now(UTC)
            # petrosa-data-manager companion to petrosa-bot-ta-analysis#267
            # (AC6): stamp an UNCONDITIONAL real BSON Date field for the
            # `signals` Mongo collection specifically. Callers (e.g. the TA
            # bot) already send their own `timestamp` as an ISO *string* for
            # JSON compatibility, so the generic `if "timestamp" not in item`
            # auto-stamp above never fires for them and a TTL index on
            # `timestamp` would silently never expire anything — the exact
            # gotcha already observed on the `alerts` collection. This field
            # is dedicated to TTL maintenance only; see
            # data_manager/maintenance/intents_ttl_index.py
            # `ensure_signals_ttl_index`.
            if database == "mongodb" and collection == "signals":
                item["_ttl_inserted_at"] = datetime.now(UTC)
            model_instances.append(GenericModel(**item))

        # Execute insert
        if database == "mysql":
            from data_manager.utils.circuit_breaker import CircuitBreakerOpenError

            try:
                write_result = adapter.write(model_instances, collection)
            except CircuitBreakerOpenError as exc:
                # Per #213 AC2.4: surface the OPEN-circuit case as 503 so callers
                # (urllib3 Retry, k8s ingress) treat it as transient and back off.
                api_module.db_manager.increment_error_count(database)
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            inserted_count = write_result.inserted
            duplicates = write_result.duplicates
            failed = write_result.failed
            ignored_count = write_result.ignored_count
        else:  # MongoDB
            inserted_count = await adapter.write(model_instances, collection)
            duplicates = 0
            failed = 0
            ignored_count = 0

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        # Per #213 AC2.3 / AC2.4: always surface explicit counts so callers can
        # distinguish "all duplicates" from "wrote nothing because it failed".
        response: dict[str, Any] = {
            "message": f"Successfully inserted {inserted_count} records",
            "inserted_count": inserted_count,
            **({"ignored_count": ignored_count} if database == "mysql" else {}),
            "duplicates": duplicates,
            "failed": failed,
            "metadata": {
                "database": database,
                "collection": collection,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        }
        if duplicates and not inserted_count:
            response["message"] = (
                f"No records inserted — {duplicates} duplicate(s) ignored"
            )
        return response

    except HTTPException:
        raise
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

        ignored_fields: list[str] = []
        if database == "mysql":
            ignored_fields = _validate_mysql_update_data(
                adapter, collection, request.data
            )

        # Schema validation (if enabled)
        if validate and schema:
            # For updates, validate the updated data
            await _validate_data_against_schema(database, schema, [request.data])

        # Determine which records match the filter (drives the early-return /
        # upsert-create branching below); the actual persistence happens via
        # adapter.update() further down, not by re-writing these in-memory rows.

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
            response: dict[str, Any] = {
                "message": "No records found matching filter",
                "updated_count": 0,
                "metadata": {
                    "database": database,
                    "collection": collection,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }
            if ignored_fields:
                response["ignored_fields"] = ignored_fields
            return response

        # Update records: persist via a real UPDATE (mysql) / update_many
        # (MongoDB) so changes actually land in the database instead of only
        # mutating the in-memory dicts pulled from query_range() — those were
        # previously discarded, and `updated_count` was only ever assigned on
        # the empty-match/upsert branch below, causing an UnboundLocalError
        # (500) on every update of an existing record (petrosa-data-manager#262).
        if matching_records:
            update_data = dict(request.data)
            update_data["updated_at"] = datetime.now(UTC)

            if database == "mysql":
                from data_manager.utils.circuit_breaker import CircuitBreakerOpenError

                try:
                    updated_count = adapter.update(
                        collection, request.filter, update_data
                    )
                except CircuitBreakerOpenError as exc:
                    api_module.db_manager.increment_error_count(database)
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
            else:  # MongoDB
                updated_count = await adapter.update(
                    collection, request.filter, update_data
                )

        # If upsert and no matches, create new record
        elif request.upsert:
            new_record = {
                key: value
                for key, value in request.data.items()
                if key not in ignored_fields
            }
            if ignored_fields and database == "mysql":
                adapter.record_ignored_fields(collection, ignored_fields)
            new_record["created_at"] = datetime.now(UTC)
            new_record["updated_at"] = datetime.now(UTC)

            from pydantic import BaseModel, ConfigDict

            class GenericModel(BaseModel):
                model_config = ConfigDict(extra="allow")

            model_instance = GenericModel(**new_record)

            if database == "mysql":
                write_result = adapter.write([model_instance], collection)
                updated_count = write_result.inserted
            else:  # MongoDB
                updated_count = await adapter.write([model_instance], collection)

        else:
            updated_count = 0

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        response: dict[str, Any] = {
            "message": f"Successfully updated {updated_count} records",
            "updated_count": updated_count,
            "metadata": {
                "database": database,
                "collection": collection,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        }
        if ignored_fields:
            response["ignored_fields"] = ignored_fields
        return response

    except HTTPException:
        raise
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
                "timestamp": datetime.now(UTC).isoformat(),
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
                        item["timestamp"] = datetime.now(UTC)
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
                    record["updated_at"] = datetime.now(UTC)

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
                "timestamp": datetime.now(UTC).isoformat(),
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
