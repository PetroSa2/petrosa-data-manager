"""HTTP API for MongoDB-backed coordination leases."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Path
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

import data_manager.api.app as api_module
from data_manager.db.repositories.lease_repository import LeaseRepository

router = APIRouter(prefix="/api/v1/leases")
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


async def _run(op: str, action):  # type: ignore[no-untyped-def]
    started = perf_counter()
    try:
        result = await action()
    except HTTPException:
        raise
    except Exception as exc:
        lease_ops_total.labels(op=op, result="error").inc()
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    finally:
        lease_op_seconds.labels(op=op).observe(perf_counter() - started)
    return result


@router.post("/{name}/acquire")
async def acquire(
    name: str = Path(...), request: LeaseRequest = Body(...)
) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run(
        "acquire", lambda: _repo().acquire(name, request.owner, request.ttl_seconds)
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
    name: str = Path(...), request: LeaseRequest = Body(...)
) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run(
        "renew", lambda: _repo().renew(name, request.owner, request.ttl_seconds)
    )
    if lease is None:
        lease_ops_total.labels(op="renew", result="lost").inc()
        holder = await _run("get", lambda: _repo().get(name))
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
    name: str = Path(...), request: ReleaseRequest = Body(...)
) -> dict[str, Any]:
    name = _validate_name(name)
    released = await _run("release", lambda: _repo().release(name, request.owner))
    lease_ops_total.labels(op="release", result="released").inc()
    return {"released": released}


@router.get("/{name}")
async def get(name: str = Path(...)) -> dict[str, Any]:
    name = _validate_name(name)
    lease = await _run("get", lambda: _repo().get(name))
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
