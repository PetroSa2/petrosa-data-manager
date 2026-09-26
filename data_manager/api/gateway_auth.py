"""Service authentication dependency for the data-manager gateway."""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from functools import lru_cache

from fastapi import HTTPException, Request
from prometheus_client import Counter

logger = logging.getLogger(__name__)

GATEWAY_REQUESTS = Counter(
    "data_manager_gateway_requests_total",
    "Generic gateway requests by caller and policy decision",
    ["service", "database", "collection", "op", "decision"],
)

_EXEMPT_PREFIXES = ("/health/",)
_EXEMPT_PATHS = frozenset({"/metrics", "/docs", "/openapi.json"})
_AUDIT_LOGGED: dict[tuple[str, str], float] = {}


@lru_cache(maxsize=1)
def _settings() -> tuple[str, dict[str, str]]:
    mode = os.environ.get("DM_AUTH_MODE", "audit").lower()
    if mode not in {"off", "audit", "enforce"}:
        logger.warning("Invalid DM_AUTH_MODE=%s; using audit", mode)
        mode = "audit"
    try:
        tokens = json.loads(os.environ.get("DM_SERVICE_TOKENS", "{}"))
    except json.JSONDecodeError:
        logger.error("DM_SERVICE_TOKENS is not valid JSON")
        tokens = {}
    return mode, tokens if isinstance(tokens, dict) else {}


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or any(
        path.startswith(prefix) for prefix in _EXEMPT_PREFIXES
    )


def _audit_unverified(service: str, path: str) -> None:
    now = time.monotonic()
    key = (service, path)
    if now - _AUDIT_LOGGED.get(key, 0) >= 300:
        logger.warning("gateway_auth_unverified service=%s path=%s", service, path)
        _AUDIT_LOGGED[key] = now


async def require_service(request: Request) -> str:
    """Authenticate a service caller according to the configured gateway mode."""
    mode, tokens = _settings()
    service = request.headers.get("X-Petrosa-Service") or "none"
    authorization = request.headers.get("Authorization", "")
    presented = authorization.removeprefix("Bearer ").strip()
    expected = tokens.get(service)
    valid = bool(
        expected and presented and hmac.compare_digest(str(expected), presented)
    )

    if _is_exempt(request.url.path) or mode == "off":
        request.state.gateway_service = service
        request.state.gateway_auth_verified = True
        return service
    if valid:
        request.state.gateway_service = service
        request.state.gateway_auth_verified = True
        return service

    if mode == "enforce":
        raise HTTPException(
            status_code=401, detail="valid service authentication required"
        )

    _audit_unverified(service, request.url.path)
    request.state.gateway_service = service
    request.state.gateway_auth_verified = False
    return service


def record_gateway_request(
    request: Request, database: str, collection: str, op: str, decision: str
) -> None:
    """Record a generic gateway decision without exposing credentials."""
    service = getattr(request.state, "gateway_service", "none")
    GATEWAY_REQUESTS.labels(service, database, collection, op, decision).inc()


def auth_mode() -> str:
    """Return the cached authentication mode for policy enforcement."""
    return _settings()[0]


__all__ = [
    "GATEWAY_REQUESTS",
    "auth_mode",
    "record_gateway_request",
    "require_service",
]
