"""Allowlist policy for generic database gateway operations."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from data_manager.api.gateway_auth import auth_mode, record_gateway_request
from data_manager.persistence_registry import REGISTRY

logger = logging.getLogger(__name__)

GENERIC_POLICY: dict[str, dict[str, set[str]]] = {
    "mongodb": {
        "signals": {"read", "insert"},
        "klines_*": {"read", "insert"},
        "funding_*": {"read", "insert"},
        "heartbeat_state": {"read", "upsert"},
        "strategy_configs_global": {"read", "insert", "upsert", "update", "delete"},
        "strategy_configs_symbol": {"read", "insert", "upsert", "update", "delete"},
        "strategy_config_audit": {"read", "insert", "upsert", "update", "delete"},
        "trading_configs_*": {"read", "insert", "upsert", "update", "delete"},
        "characterization_artifacts": {"read", "insert"},
        "config_rate_limits": {"read", "insert"},
        "strategy_lifecycle_events": {"read", "insert"},
    },
    "mysql": {
        "positions": {"read", "insert", "update", "upsert"},
        "daily_pnl": {"read", "insert", "update", "upsert"},
        "*": {"read"},
    },
}

_POLICY_REGISTRY_KEYS = {
    key for key in REGISTRY if key not in GENERIC_POLICY["mongodb"]
}
DENY_BY_DESIGN = frozenset(_POLICY_REGISTRY_KEYS)


def _operations_for(database: str, collection: str) -> set[str]:
    database_policy = GENERIC_POLICY.get(database, {})
    if collection in database_policy:
        return database_policy[collection]
    for key, operations in database_policy.items():
        if key.endswith("*") and collection.startswith(key[:-1]):
            return operations
    return set()


def check_generic(database: str, collection: str, op: str) -> bool:
    """Return whether a generic gateway operation is allowlisted."""
    return op in _operations_for(database, collection) or (
        database == "mysql"
        and collection not in {"positions", "daily_pnl"}
        and op == "read"
    )


def authorize_generic(
    request: Request, database: str, collection: str, op: str
) -> None:
    """Record and enforce a generic route policy decision."""
    allowed = check_generic(database, collection, op)
    verified = getattr(request.state, "gateway_auth_verified", True)
    decision = "allowed" if allowed else "denied"
    if not verified and auth_mode() == "audit":
        decision = "unverified" if allowed else "denied"
    record_gateway_request(request, database, collection, op, decision)
    if not allowed:
        logger.warning(
            "gateway_policy_denied database=%s collection=%s op=%s",
            database,
            collection,
            op,
        )
        if auth_mode() == "enforce":
            raise HTTPException(
                status_code=403,
                detail=f"collection not allowed: {database}.{collection}:{op}",
            )


__all__ = ["DENY_BY_DESIGN", "GENERIC_POLICY", "authorize_generic", "check_generic"]
