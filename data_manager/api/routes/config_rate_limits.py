"""Configuration change rate-limit API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

import data_manager.api.app as api_module

try:
    from petrosa_otel import ConfigRateLimiter
except ImportError:  # pragma: no cover - depends on the optional deployment package
    ConfigRateLimiter = None


router = APIRouter(prefix="/api/v1/config/rate-limits", tags=["Configuration"])


class RateLimitOptions(BaseModel):
    """Optional per-request rate-limit settings."""

    per_agent_limit: int = Field(10, ge=0)
    per_endpoint_limit: int = Field(30, ge=0)
    global_limit: int = Field(100, ge=0)
    window_seconds: int = Field(3600, ge=1)
    cooldown_seconds: int = Field(300, ge=0)


class RateLimitCheckRequest(BaseModel):
    """Request to check whether a configuration change is allowed."""

    service: str = Field(..., pattern=r"^[a-z0-9-]{1,64}$")
    changed_by: str = Field(..., max_length=256)
    endpoint: str = Field(..., max_length=256)
    allow_emergency: bool = True
    limits: RateLimitOptions | None = None


class RateLimitRecordRequest(BaseModel):
    """Request to record a configuration change."""

    service: str = Field(..., pattern=r"^[a-z0-9-]{1,64}$")
    changed_by: str = Field(..., max_length=256)
    endpoint: str = Field(..., max_length=256)
    success: bool = True


def _limiter(body: RateLimitCheckRequest | RateLimitRecordRequest) -> Any:
    manager = api_module.db_manager
    adapter = getattr(manager, "mongodb_adapter", None) if manager else None
    if adapter is None or ConfigRateLimiter is None:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

    options = (
        body.limits.model_dump()
        if isinstance(body, RateLimitCheckRequest) and body.limits
        else {}
    )
    return ConfigRateLimiter(
        mongodb_client=adapter,
        service_name=body.service,
        **options,
        enabled=True,
    )


@router.post("/check")
async def check_rate_limit(body: RateLimitCheckRequest) -> dict[str, Any]:
    """Check the shared configuration-change quota."""

    limiter = _limiter(body)
    try:
        return await limiter.check_rate_limit(
            changed_by=body.changed_by,
            endpoint=body.endpoint,
            allow_emergency=body.allow_emergency,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc


@router.post("/record", status_code=status.HTTP_201_CREATED)
async def record_change(body: RateLimitRecordRequest) -> dict[str, bool]:
    """Record a configuration change in the shared rate-limit collection."""

    limiter = _limiter(body)
    try:
        await limiter.record_change(
            changed_by=body.changed_by,
            endpoint=body.endpoint,
            success=body.success,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"recorded": True}
