"""HTTP API for MongoDB-backed coordination leases."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import traceback
from collections import deque
from datetime import UTC, datetime
from statistics import median
from time import monotonic, perf_counter
from typing import Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field
from pymongo.errors import ExecutionTimeout, NetworkTimeout, ServerSelectionTimeoutError

import data_manager.api.app as api_module
from data_manager.api.routes.health import readiness_snapshot
from data_manager.db.repositories.lease_repository import (
    LEASES_COLLECTION,
    LeaseRepository,
)
from data_manager.observability_metrics import ATLAS_LOAD_CACHE_EVENTS
from data_manager.utils.event_loop_monitor import event_loop_snapshot

router = APIRouter(prefix="/api/v1/leases")
logger = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
_MONGODB_CREDENTIALS_RE = re.compile(r"(mongodb(?:\+srv)?://)[^/@]+@")
lease_op_seconds = Histogram(
    "data_manager_lease_op_seconds", "Lease operation latency", ["op"]
)
lease_ops_total = Counter(
    "data_manager_lease_ops_total", "Lease operation outcomes", ["op", "result"]
)
_LEASE_DIAGNOSTIC_WINDOW_SECONDS = 3 * 60 * 60
_LEASE_DIAGNOSTIC_MAX_RECORDS = 20_000
_lease_diagnostics: deque[dict[str, Any]] = deque(maxlen=_LEASE_DIAGNOSTIC_MAX_RECORDS)
_LEASE_GET_CACHE_SECONDS = 0.25
_lease_get_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_lease_get_inflight: dict[str, asyncio.Task[dict[str, Any] | None]] = {}
_lease_get_lock = asyncio.Lock()


class LeaseRequest(BaseModel):
    owner: str = Field(..., min_length=1)
    ttl_seconds: int = Field(..., ge=1, le=300)


class ReleaseRequest(BaseModel):
    owner: str = Field(..., min_length=1)


def _repo() -> LeaseRepository:
    manager = api_module.db_manager
    if not manager or not getattr(manager, "mongodb_adapter", None):
        raise RuntimeError("MongoDB unavailable")
    return LeaseRepository(mongodb_adapter=manager.mongodb_adapter)


async def _cached_get(name: str) -> dict[str, Any] | None:
    now = monotonic()
    cached = _lease_get_cache.get(name)
    if cached and now - cached[0] < _LEASE_GET_CACHE_SECONDS:
        ATLAS_LOAD_CACHE_EVENTS.add(1, {"route": "lease_get", "result": "hit"})
        return cached[1]

    async with _lease_get_lock:
        now = monotonic()
        cached = _lease_get_cache.get(name)
        if cached and now - cached[0] < _LEASE_GET_CACHE_SECONDS:
            ATLAS_LOAD_CACHE_EVENTS.add(1, {"route": "lease_get", "result": "hit"})
            return cached[1]
        task = _lease_get_inflight.get(name)
        if task is None:
            task = asyncio.create_task(_repo().get(name))
            _lease_get_inflight[name] = task
            owner = True
        else:
            owner = False

    try:
        result = await task
    finally:
        if owner:
            async with _lease_get_lock:
                _lease_get_inflight.pop(name, None)
    if owner and result is not None:
        _lease_get_cache[name] = (monotonic(), result)
        ATLAS_LOAD_CACHE_EVENTS.add(1, {"route": "lease_get", "result": "backend"})
    else:
        ATLAS_LOAD_CACHE_EVENTS.add(1, {"route": "lease_get", "result": "shared"})
    return result


def _invalidate_get_cache(name: str) -> None:
    _lease_get_cache.pop(name, None)


def _validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=422, detail="invalid lease name")
    return name


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _diagnostic(
    error: BaseException | None, queue_wait_ms: float, handler_ms: float
) -> str:
    if error is not None:
        if isinstance(
            error,
            asyncio.TimeoutError
            | ExecutionTimeout
            | NetworkTimeout
            | ServerSelectionTimeoutError,
        ):
            return "mongo_wait_timeout"
        if isinstance(error, RuntimeError) and "unavailable" in str(error).lower():
            return "readiness_backend_stall"
        return "mongo_backend_error"
    if queue_wait_ms > handler_ms:
        return "event_loop_queueing"
    return "handler_execution"


def _error_category(error: BaseException | None) -> str:
    if error is None:
        return "success"
    if isinstance(
        error,
        asyncio.TimeoutError
        | ExecutionTimeout
        | NetworkTimeout
        | ServerSelectionTimeoutError,
    ):
        return "timeout"
    if isinstance(error, RuntimeError) and "unavailable" in str(error).lower():
        return "readiness_failure"
    return "backend_failure"


def _error_class(error: BaseException | None) -> str:
    return type(error).__name__ if error is not None else "success"


def _lease_context() -> dict[str, str]:
    manager = api_module.db_manager
    adapter = getattr(manager, "mongodb_adapter", None)
    return {
        "lease_collection": LEASES_COLLECTION,
        "lease_database": str(getattr(adapter, "db_name", "unknown")),
    }


def _safe_error_text(error: BaseException | str) -> str:
    return _MONGODB_CREDENTIALS_RE.sub(r"\1<redacted>@", str(error))


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * percentile)]


def _prune_diagnostics(now: datetime) -> None:
    cutoff = now.timestamp() - _LEASE_DIAGNOSTIC_WINDOW_SECONDS
    while _lease_diagnostics:
        timestamp = datetime.fromisoformat(
            _lease_diagnostics[0]["timestamp"]
        ).timestamp()
        if timestamp >= cutoff:
            break
        _lease_diagnostics.popleft()


def _diagnostic_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    queue_wait = [float(record["lease_queue_wait_ms"]) for record in records]
    handler = [float(record["lease_handler_ms"]) for record in records]
    if not queue_wait:
        empty = {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
        return {"count": 0, "queue_wait_ms": empty, "handler_ms": empty}

    def stats(values: list[float]) -> dict[str, float | int]:
        return {
            "count": len(values),
            "min": min(values),
            "p50": median(values),
            "p95": _percentile(values, 0.95),
            "max": max(values),
        }

    return {
        "count": len(records),
        "queue_wait_ms": stats(queue_wait),
        "handler_ms": stats(handler),
    }


def _store_diagnostic(
    *,
    op: str,
    queue_wait_ms: float,
    handler_ms: float,
    mongo_wait_ms: float,
    error: BaseException | None,
    timestamp: datetime,
    result_error_class: str | None = None,
) -> None:
    readiness = readiness_snapshot()
    loop = event_loop_snapshot()
    error_class = result_error_class or _error_class(error)
    error_category = (
        _error_category(error)
        if error is not None
        else (result_error_class or "success")
    )
    context = _lease_context()
    if error is not None and readiness["mongodb"] == "unknown":
        readiness["mongodb"] = "failed"
    record = {
        "timestamp": timestamp.isoformat(),
        "lease_op": op,
        "lease_outcome": "error"
        if error is not None or result_error_class
        else "success",
        "lease_error_class": error_class,
        "lease_queue_wait_ms": queue_wait_ms,
        "lease_handler_ms": handler_ms,
        "lease_mongo_wait_ms": mongo_wait_ms,
        "lease_error_category": error_category,
        "lease_error_message": (
            _safe_error_text(error)[:1000] if error is not None else None
        ),
        "lease_error_traceback": (
            _safe_error_text("".join(traceback.format_exception(error)))[-4000:]
            if error is not None
            else None
        ),
        **context,
        "lease_timed_out": error_category == "timeout",
        "lease_diagnostic": _diagnostic(error, queue_wait_ms, handler_ms),
        "lease_mongo_readiness": readiness["mongodb"],
        "lease_mysql_backend": readiness["mysql"],
        "lease_event_loop_state": loop["state"],
        "lease_event_loop_lag_ms": loop["lag_ms"],
    }
    _prune_diagnostics(timestamp)
    _lease_diagnostics.append(record)
    logger.info(json.dumps({"event": "lease_timing", **record}, sort_keys=True))


def _query_diagnostics(
    since: datetime | None,
    until: datetime | None,
    operation: str | None,
    outcome: str | None,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    _prune_diagnostics(now)
    since_ts = since.timestamp() if since else None
    until_ts = until.timestamp() if until else None
    return [
        record
        for record in _lease_diagnostics
        if (
            since_ts is None
            or datetime.fromisoformat(record["timestamp"]).timestamp() >= since_ts
        )
        and (
            until_ts is None
            or datetime.fromisoformat(record["timestamp"]).timestamp() <= until_ts
        )
        and (operation is None or record["lease_op"] == operation)
        and (outcome is None or record["lease_outcome"] == outcome)
    ]


async def _run(request: Request | None, op: str, action, result_error_class=None):
    started = perf_counter()
    received_at = getattr(getattr(request, "state", None), "received_at", started)
    queue_wait = started - received_at
    mongo_started = perf_counter()
    error: BaseException | None = None
    try:
        result = await action()
    except HTTPException:
        raise
    except Exception as exc:
        error = exc
        lease_ops_total.labels(op=op, result="error").inc()
        context = _lease_context()
        logger.error(
            "Lease operation failed op=%s collection=%s database=%s "
            "exception=%s message=%s",
            op,
            context["lease_collection"],
            context["lease_database"],
            type(exc).__name__,
            _safe_error_text(exc),
            extra=context,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    finally:
        handler_time = perf_counter() - started
        mongo_wait = perf_counter() - mongo_started
        queue_wait_ms = round(queue_wait * 1000, 2)
        handler_ms = round(handler_time * 1000, 2)
        lease_op_seconds.labels(op=op).observe(handler_time)
        _store_diagnostic(
            op=op,
            queue_wait_ms=queue_wait_ms,
            handler_ms=handler_ms,
            mongo_wait_ms=round(mongo_wait * 1000, 2),
            error=error,
            timestamp=datetime.now(UTC),
            result_error_class=(
                result_error_class(result)
                if error is None and result_error_class
                else None
            ),
        )
    return result


@router.get("/diagnostics")
async def diagnostics(
    since: datetime | None = None,
    until: datetime | None = None,
    operation: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    """Return bounded lease timing records and latency summaries."""
    if since and until and since > until:
        raise HTTPException(status_code=422, detail="since must not be after until")
    records = _query_diagnostics(since, until, operation, outcome)
    window_end = datetime.now(UTC)
    window_start = window_end.timestamp() - _LEASE_DIAGNOSTIC_WINDOW_SECONDS
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(
            f"{record['lease_op']}:{record['lease_outcome']}:{record['lease_error_class']}",
            [],
        ).append(record)
    return {
        "window_seconds": _LEASE_DIAGNOSTIC_WINDOW_SECONDS,
        "window_start": datetime.fromtimestamp(window_start, UTC).isoformat(),
        "window_end": window_end.isoformat(),
        "count": len(records),
        "records": records,
        "summary": {key: _diagnostic_summary(value) for key, value in grouped.items()},
    }


@router.post("/{name}/acquire")
async def acquire(
    name: str = Path(...),
    request: LeaseRequest = Body(...),
    http_request: Request = cast(Request, None),
) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run(
        http_request,
        "acquire",
        lambda: _repo().acquire(name, request.owner, request.ttl_seconds),
    )
    _invalidate_get_cache(name)
    lease_ops_total.labels(
        op="acquire", result="acquired" if lease["acquired"] else "contended"
    ).inc()
    return {
        "acquired": lease["acquired"],
        "name": name,
        "owner": lease.get("owner"),
        "expires_at": _iso(lease["expires_at"]),
        "fencing_token": lease.get("fencing_token", 0),
    }


@router.post("/{name}/renew")
async def renew(
    name: str = Path(...),
    request: LeaseRequest = Body(...),
    http_request: Request = cast(Request, None),
) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run(
        http_request,
        "renew",
        lambda: _repo().renew(name, request.owner, request.ttl_seconds),
        lambda result: "renew_failed" if result is None else None,
    )
    _invalidate_get_cache(name)
    if lease is None:
        lease_ops_total.labels(op="renew", result="lost").inc()
        raise HTTPException(status_code=409, detail={"renewed": False, "owner": None})
    lease_ops_total.labels(op="renew", result="renewed").inc()
    return {
        "renewed": True,
        "expires_at": _iso(lease["expires_at"]),
        "fencing_token": lease["fencing_token"],
    }


@router.post("/{name}/release")
async def release(
    name: str = Path(...),
    request: ReleaseRequest = Body(...),
    http_request: Request = cast(Request, None),
) -> dict[str, Any]:
    name = _validate_name(name)
    released = await _run(
        http_request, "release", lambda: _repo().release(name, request.owner)
    )
    _invalidate_get_cache(name)
    lease_ops_total.labels(op="release", result="released").inc()
    return {"released": released}


@router.get("/{name}")
async def get(
    name: str = Path(...), http_request: Request = cast(Request, None)
) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run(http_request, "get", lambda: _cached_get(name))
    if lease is None:
        raise HTTPException(status_code=404, detail="lease not found")
    held = lease["expires_at"] >= datetime.now(UTC)
    return {
        "name": name,
        "owner": lease["owner"],
        "expires_at": _iso(lease["expires_at"]),
        "held": held,
        "fencing_token": lease["fencing_token"],
    }
