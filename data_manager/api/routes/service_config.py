"""Per-service runtime configuration API."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from data_manager.db.repositories.service_config_repository import (
    ServiceConfigRepository,
    ServiceConfigVersionConflict,
)

router = APIRouter(prefix="/api/v1/config/services", tags=["Service Configuration"])
db_manager: Any = None
_SERVICE_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_RESERVED = {"history", "rollback"}


class ServiceConfigRequest(BaseModel):
    value: Any
    changed_by: str = Field(min_length=1)
    reason: str | None = None
    expected_version: int | None = Field(None, ge=0)


def set_database_manager(manager: Any) -> None:
    global db_manager
    db_manager = manager


def _validate(service: str, key: str | None = None) -> None:
    if not _SERVICE_RE.fullmatch(service) or service in _RESERVED:
        raise HTTPException(status_code=422, detail="invalid service")
    if key is not None and not _KEY_RE.fullmatch(key):
        raise HTTPException(status_code=422, detail="invalid key")


def _repository() -> ServiceConfigRepository:
    adapter = getattr(db_manager, "mongodb_adapter", None) if db_manager else None
    if adapter is None or getattr(adapter, "db", None) is None:
        raise HTTPException(status_code=503, detail="Database manager not available")
    return ServiceConfigRepository(adapter)


def _jsonable(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    result.pop("_id", None)
    for field in ("updated_at", "changed_at"):
        if isinstance(result.get(field), datetime):
            result[field] = result[field].isoformat()
    return result


@router.get("/{service}")
async def get_service_config(service: str = Path(...)):
    _validate(service)
    try:
        documents = await _repository().list(service)
        return {
            "service": service,
            "keys": {
                document["key"]: {
                    key: value
                    for key, value in _jsonable(document).items()
                    if key not in {"service", "key"}
                }
                for document in documents
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{service}/keys/{key}")
async def get_service_config_key(service: str, key: str):
    _validate(service, key)
    try:
        document = await _repository().get(service, key)
        if document is None:
            raise HTTPException(status_code=404, detail="configuration key not found")
        return _jsonable(document)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/{service}/keys/{key}")
async def put_service_config(service: str, key: str, request: ServiceConfigRequest):
    _validate(service, key)
    if len(json.dumps(request.value, separators=(",", ":"))) > 64 * 1024:
        raise HTTPException(status_code=413, detail="value exceeds 64 KB")
    try:
        document = await _repository().put(
            service,
            key,
            request.value,
            request.changed_by,
            request.reason,
            request.expected_version,
        )
        return _jsonable(document)
    except ServiceConfigVersionConflict as exc:
        detail: dict[str, Any] = {"detail": "version conflict"}
        if exc.current_version is not None:
            detail["current_version"] = exc.current_version
        raise HTTPException(status_code=409, detail=detail) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{service}/audit")
async def get_service_config_audit(
    service: str,
    key: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    _validate(service, key)
    try:
        entries = await _repository().audit_entries(service, key, limit)
        return {"entries": [_jsonable(entry) for entry in entries]}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
