"""HTTP API for MongoDB-backed coordination leases."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field
from pymongo.errors import ExecutionTimeout, NetworkTimeout, ServerSelectionTimeoutError

import data_manager.api.app as api_module
from data_manager.db.repositories.lease_repository import LeaseRepository

router = APIRouter(prefix="/api/v1/leases")
logger = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
lease_op_seconds = Histogram(
    "data_manager_lease_op_seconds", "Lease operation latency", ["op"]
)
lease_ops_total = Counter(
    "data_manager_lease_ops_total", "Lease operation outcomes", ["op", "result"]
)


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


async def _run(request: Request | None, op: str, action):
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
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    finally:
        handler_time = perf_counter() - started
        mongo_wait = perf_counter() - mongo_started
        queue_wait_ms = round(queue_wait * 1000, 2)
        handler_ms = round(handler_time * 1000, 2)
        lease_op_seconds.labels(op=op).observe(handler_time)
        logger.info(
            "lease timing",
            extra={
                "lease_op": op,
                "lease_queue_wait_ms": queue_wait_ms,
                "lease_handler_ms": handler_ms,
                "lease_mongo_wait_ms": round(mongo_wait * 1000, 2),
                "lease_timed_out": isinstance(
                    error,
                    asyncio.TimeoutError
                    | ExecutionTimeout
                    | NetworkTimeout
                    | ServerSelectionTimeoutError,
                ),
                "lease_diagnostic": _diagnostic(error, queue_wait_ms, handler_ms),
            },
        )
    return result


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
    )
    if lease is None:
        lease_ops_total.labels(op="renew", result="lost").inc()
        holder = await _run(http_request, "get", lambda: _repo().get(name))
        raise HTTPException(
            status_code=409,
            detail={"renewed": False, "owner": holder.get("owner") if holder else None},
        )
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
    lease_ops_total.labels(op="release", result="released").inc()
    return {"released": released}


@router.get("/{name}")
async def get(
    name: str = Path(...), http_request: Request = cast(Request, None)
) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run(http_request, "get", lambda: _repo().get(name))
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
