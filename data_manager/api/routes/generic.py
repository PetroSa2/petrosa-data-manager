"""
Generic CRUD API endpoints for dynamic database/collection operations.
"""

import asyncio
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
from data_manager.api.gateway_policy import authorize_generic
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


def _require_equality_filter(
    filter_dict: dict[str, Any] | None, operation: str
) -> dict[str, Any]:
    """Return ``filter_dict``, or raise 400 unless it is a non-empty equality match.

    The same rule as ``MongoDBAdapter._build_equality_query``, applied at the
    HTTP boundary so a bad filter is a 400 on both backends. Without it, an
    empty filter was a 500, and on MySQL an operator key such as ``$or`` was
    silently dropped by ``update()``, widening the match (data-manager#378).
    """
    if not filter_dict:
        raise HTTPException(
            status_code=400, detail=f"{operation} filter cannot be empty"
        )
    for key, value in filter_dict.items():
        if key.startswith("$") or isinstance(value, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{operation} filter must be a flat equality match, "
                    f"got operator-like entry {key!r}"
                ),
            )
    return filter_dict


async def _apply_update(
    adapter: Any,
    database: str,
    collection: str,
    filter_dict: dict[str, Any],
    data: dict[str, Any],
    *,
    upsert: bool,
) -> tuple[int, bool]:
    """Persist one generic update and return ``(updated_count, upserted)``.

    Shared by ``PUT`` and batch ``update`` (data-manager#378 items 5 and 8).
    A MongoDB upsert is one atomic ``update_one(upsert=True)``. MySQL runs
    its blocking calls in a worker thread: UPDATE first, then INSERT when
    nothing matched and ``upsert`` is set. The UPDATE already records any
    unknown columns in the ignored-field metric, and the INSERT skips them.
    """
    now = datetime.now(UTC)
    update_data = {**data, "updated_at": now}
    if database != "mysql":
        if upsert:
            result = await adapter.upsert_one(collection, filter_dict, update_data)
            upserted = result["upserted_id"] is not None
            return result["modified"] + int(upserted), upserted
        return await adapter.update(collection, filter_dict, update_data), False

    updated_count = await asyncio.to_thread(
        adapter.update, collection, filter_dict, update_data
    )
    if updated_count or not upsert:
        return updated_count, False

    from pydantic import BaseModel, ConfigDict

    class GenericModel(BaseModel):
        model_config = ConfigDict(extra="allow")

    record = GenericModel(**{"created_at": now, **filter_dict, **update_data})
    result = await asyncio.to_thread(adapter.write, [record], collection)
    if result.inserted:
        return int(result.inserted), True
    if result.duplicates:
        # Another writer inserted the row between our UPDATE and INSERT, and
        # INSERT IGNORE dropped ours: update that row instead of losing it.
        updated_count = await asyncio.to_thread(
            adapter.update, collection, filter_dict, update_data
        )
        return updated_count, False
    raise DatabaseError(
        f"MySQL upsert insert into {collection} failed ({result.failed} failed)"
    )


async def _apply_delete(
    adapter: Any, database: str, collection: str, filter_dict: dict[str, Any]
) -> int:
    """Delete the records matching ``filter_dict`` and return the real count."""
    if database == "mysql":
        return await asyncio.to_thread(adapter.delete, collection, filter_dict)
    return await adapter.delete_many(collection, filter_dict)


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
            records, total_count = await asyncio.to_thread(
                adapter.find_paginated,
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
async def legacy_query(
    request: dict[str, Any], http_request: Request
) -> dict[str, Any]:
    """
    Legacy query endpoint used by older versions of data-extractor.
    Forwards to generic CRUD logic.
    """
    database = request.get("database", "mongodb")
    collection = request.get("collection")
    if not collection:
        raise HTTPException(status_code=400, detail="Collection name required")
    authorize_generic(http_request, database, collection, "read")

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
async def legacy_insert(
    request: dict[str, Any], http_request: Request
) -> dict[str, Any]:
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
        http_request=http_request,
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


class FindOneAndUpdateRequest(BaseModel):
    """Equality compare-and-set request (MongoDB only)."""

    filter: dict[str, Any] = Field(
        ..., description="Expected current values; the document must match all"
    )
    set: dict[str, Any] = Field(..., description="Fields to $set when it matches")
    upsert: bool = Field(False, description="Insert filter+set when nothing matches")


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
    authorize_generic(request, database, collection, "read")
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
    http_request: Request,
    schema: str | None = Query(None, description="Schema name for validation"),
    validate: bool = Query(False, description="Enable schema validation"),
) -> dict[str, Any]:
    """
    Insert records into a database collection.

    Supports single record or batch insertion.
    """
    authorize_generic(http_request, database, collection, "insert")
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
    http_request: Request,
    schema: str | None = Query(None, description="Schema name for validation"),
    validate: bool = Query(False, description="Enable schema validation"),
) -> dict[str, Any]:
    """
    Update records in a database collection.

    Supports filtering and upsert operations.
    """
    authorize_generic(
        http_request, database, collection, "upsert" if request.upsert else "update"
    )
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        adapter = _get_adapter(database)
        _require_equality_filter(request.filter, "Update")

        ignored_fields: list[str] = []
        if database == "mysql":
            # get_column_names() may reflect the table: blocking MySQL I/O.
            ignored_fields = await asyncio.to_thread(
                _validate_mysql_update_data, adapter, collection, request.data
            )

        # Schema validation (if enabled)
        if validate and schema:
            # For updates, validate the updated data
            await _validate_data_against_schema(database, schema, [request.data])

        updated_count, upserted = await _apply_update(
            adapter,
            database,
            collection,
            request.filter,
            request.data,
            upsert=request.upsert,
        )

        # Track metrics
        api_module.db_manager.increment_query_count(database)

        response: dict[str, Any] = {
            "message": f"Successfully updated {updated_count} records",
            "updated_count": updated_count,
            "upserted": upserted,
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
    except CircuitBreakerOpenError as exc:
        # Per #213 AC2.4: an open MySQL circuit is transient, so 503, not 500.
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as e:
        logger.error(f"Error updating {database}.{collection}: {e}", exc_info=True)
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/{database}/{collection}/find-one-and-update")
async def find_one_and_update(
    database: str,
    collection: str,
    request: FindOneAndUpdateRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Atomic equality compare-and-set on one MongoDB document.

    Put the expected current values in ``filter``. ``set`` is applied only if
    a document still matches them, in one ``findOneAndUpdate``. ``matched`` is
    false when nothing matched, or, with ``upsert``, when a document with the
    same unique key exists but did not match (a CAS conflict).
    """
    authorize_generic(
        http_request, database, collection, "upsert" if request.upsert else "update"
    )
    if database != "mongodb":
        raise HTTPException(
            status_code=400, detail="find-one-and-update is MongoDB-only"
        )
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")
    _require_equality_filter(request.filter, "find-one-and-update")
    if not request.set:
        raise HTTPException(
            status_code=400, detail="find-one-and-update set cannot be empty"
        )
    try:
        document = await _get_adapter(database).find_one_and_update(
            collection, request.filter, request.set, upsert=request.upsert
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error in find-one-and-update on {database}.{collection}: {e}",
            exc_info=True,
        )
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e)) from e
    api_module.db_manager.increment_query_count(database)
    return {"matched": document is not None, "document": document}


@router.delete("/api/v1/{database}/{collection}")
async def delete_records(
    database: str,
    collection: str,
    request: DeleteRequest,
    http_request: Request,
) -> dict[str, Any]:
    """
    Delete records from a database collection.

    Supports filtering to identify records to delete.
    """
    authorize_generic(http_request, database, collection, "delete")
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    try:
        adapter = _get_adapter(database)
        _require_equality_filter(request.filter, "Delete")
        deleted_count = await _apply_delete(
            adapter, database, collection, request.filter
        )

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

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting from {database}.{collection}: {e}", exc_info=True)
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/{database}/{collection}/batch")
async def batch_operations(
    database: str,
    collection: str,
    request: BatchRequest,
    http_request: Request,
) -> dict[str, Any]:
    """
    Perform batch operations on a database collection.

    Supports bulk insert, update, and delete operations.
    """
    authorize_generic(http_request, database, collection, "batch")
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database manager not available")

    if len(request.operations) > constants.API_MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size exceeds maximum of {constants.API_MAX_BATCH_SIZE}",
        )

    try:
        adapter = _get_adapter(database)
        planned = await _plan_batch(adapter, database, collection, request.operations)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error validating batch on {database}.{collection}: {e}", exc_info=True
        )
        api_module.db_manager.increment_error_count(database)
        raise HTTPException(status_code=500, detail=str(e))

    # Sub-operations are not transactional: a failure part-way leaves the
    # earlier ones applied, so the error reports exactly what completed.
    results: list[dict[str, Any]] = []
    for index, (op_type, operation) in enumerate(planned):
        try:
            if op_type == "insert":
                count = await _batch_insert(adapter, database, collection, operation)
            elif op_type == "update":
                count, _ = await _apply_update(
                    adapter,
                    database,
                    collection,
                    operation["filter"],
                    operation["data"],
                    upsert=False,
                )
            else:
                count = await _apply_delete(
                    adapter, database, collection, operation["filter"]
                )
        except Exception as e:
            logger.error(
                f"Batch operation {index} ({op_type}) failed on "
                f"{database}.{collection}: {e}",
                exc_info=True,
            )
            api_module.db_manager.increment_error_count(database)
            raise HTTPException(
                # Per #213 AC2.4: an open MySQL circuit is transient, so 503.
                status_code=503 if isinstance(e, CircuitBreakerOpenError) else 500,
                detail={
                    "message": f"Batch operation {index} ({op_type}) failed: {e}",
                    "failed_operation_index": index,
                    "completed_results": results,
                },
            ) from e
        results.append({"type": op_type, "count": count})

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


async def _plan_batch(
    adapter: Any, database: str, collection: str, operations: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Validate every batch operation before any runs; return ``(type, operation)``.

    Raises 400 (422 for a MySQL update with no usable columns) on the first
    bad operation, so a malformed batch has no side effects. An unknown
    ``type`` used to be skipped silently while the batch reported success.
    """
    planned: list[tuple[str, dict[str, Any]]] = []
    for index, operation in enumerate(operations):
        op_type = operation.get("type", "insert")
        label = f"Batch operation {index} ({op_type})"
        if op_type == "insert":
            data = operation.get("data", [])
            items = data if isinstance(data, list) else [data]
            if not all(isinstance(item, dict) for item in items):
                raise HTTPException(
                    status_code=400, detail=f"{label}: data must be objects"
                )
        elif op_type in {"update", "delete"}:
            _require_equality_filter(operation.get("filter"), label)
            if op_type == "update":
                data = operation.get("data")
                if not isinstance(data, dict) or not data:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{label}: data must be a non-empty object",
                    )
                if database == "mysql":
                    await asyncio.to_thread(
                        _validate_mysql_update_data, adapter, collection, data
                    )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"{label}: unsupported type; use insert, update or delete",
            )
        planned.append((op_type, operation))
    return planned


async def _batch_insert(
    adapter: Any, database: str, collection: str, operation: dict[str, Any]
) -> int:
    """Insert one batch ``insert`` operation's records and return the count."""
    from pydantic import BaseModel, ConfigDict

    class GenericModel(BaseModel):
        # Without extra="allow", pydantic v2 drops every field and the batch
        # inserted empty documents.
        model_config = ConfigDict(extra="allow")

    data = operation.get("data", [])
    model_instances = []
    for item in data if isinstance(data, list) else [data]:
        if "timestamp" not in item:
            item["timestamp"] = datetime.now(UTC)
        model_instances.append(GenericModel(**item))

    if database == "mysql":
        return await asyncio.to_thread(adapter.write, model_instances, collection)
    return await adapter.write(model_instances, collection)


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
