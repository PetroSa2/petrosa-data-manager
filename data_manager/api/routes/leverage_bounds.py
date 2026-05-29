"""Leverage-bounds dashboard endpoints (#182, P1.5-AC6.b/c/d, FR61).

Operator surface for the per-strategy + aggregate leverage bounds:

  * ``GET /api/dashboard/leverage-bounds`` — current (latest version).
  * ``GET /api/dashboard/leverage-bounds/{version}`` — exact version.
  * ``GET /api/dashboard/leverage-bounds/versions`` — version index for
    the history pane.
  * ``PUT /api/dashboard/leverage-bounds`` — write a new version (server
    assigns the next monotonic ``v<n>``). Side effects:
      1. Persist the new ``LeverageBounds`` document.
      2. Compute :class:`LeverageBoundsDiff` against the previous version
         and write it to the ``leverage_bounds_audit`` collection
         (AC6.d, FR12).
      3. Publish ``cio.config.leverage_bounds.updated`` on NATS so the
         CIO consumer (separate sibling ticket) can hot-reload (AC6.c).

The NATS publisher is provided via :func:`set_leverage_bounds_publisher`
so the route stays testable: at app boot, ``data_manager/main.py`` calls
this once with a ``_DeferredNatsClient`` instance; tests inject a
recording stub. When no publisher is wired, the route still completes
successfully (storage + audit happen) but logs that NATS was skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, HTTPException, Path

import data_manager.api.app as api_module
from data_manager.db.repositories.leverage_bounds_repository import (
    LeverageBoundsRepository,
)
from data_manager.models.leverage_bounds import (
    LeverageBounds,
    LeverageBoundsDiff,
    LeverageBoundsPutRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


LEVERAGE_BOUNDS_AUDIT_COLLECTION = "leverage_bounds_audit"
LEVERAGE_BOUNDS_NATS_SUBJECT = "cio.config.leverage_bounds.updated"


class _NATSPublisher(Protocol):
    async def publish(self, subject: str, payload: bytes) -> None: ...


_publisher: _NATSPublisher | None = None


def set_leverage_bounds_publisher(publisher: _NATSPublisher | None) -> None:
    """Wire the NATS publisher used by the PUT route (call once at app boot)."""
    global _publisher
    _publisher = publisher


def _get_publisher() -> _NATSPublisher | None:
    return _publisher


def _require_mongo():  # type: ignore[no-untyped-def]
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "title": "MongoDB unavailable",
                "detail": "data-manager is not connected to MongoDB",
            },
        )
    return api_module.db_manager.mongodb_adapter


def _repo() -> LeverageBoundsRepository:
    mongo = _require_mongo()
    return LeverageBoundsRepository(mongodb_adapter=mongo)


@router.get("/api/dashboard/leverage-bounds")
async def get_latest_leverage_bounds() -> dict[str, Any]:
    """Latest version. Returns 404 if no bounds have been written yet."""
    repo = _repo()
    bounds = await repo.get_latest()
    if bounds is None:
        raise HTTPException(
            status_code=404,
            detail={
                "title": "leverage_bounds not configured",
                "detail": "no LeverageBounds document has been written yet",
            },
        )
    return bounds.model_dump(mode="json")


@router.get("/api/dashboard/leverage-bounds/versions")
async def list_leverage_bounds_versions(
    limit: int = 50,
) -> dict[str, Any]:
    """Version index (descending) for the dashboard history pane."""
    repo = _repo()
    versions = await repo.list_versions(limit=limit)
    return {"versions": versions, "count": len(versions)}


@router.get("/api/dashboard/leverage-bounds/{version}")
async def get_leverage_bounds_version(
    version: int = Path(..., ge=1, description="1-based version number"),
) -> dict[str, Any]:
    """Exact-version lookup. Returns 404 if the version does not exist."""
    repo = _repo()
    bounds = await repo.get_version(version)
    if bounds is None:
        raise HTTPException(
            status_code=404,
            detail={
                "title": "leverage_bounds version not found",
                "detail": f"no document at v{version}",
            },
        )
    return bounds.model_dump(mode="json")


async def _persist_audit_event(
    mongo,  # type: ignore[no-untyped-def]
    *,
    diff: LeverageBoundsDiff,
    new_bounds: LeverageBounds,
) -> None:
    """Persist the diff to the audit-trail collection (AC6.d, FR12)."""
    doc = {
        "kind": "leverage_bounds_update",
        "from_version": diff.from_version,
        "to_version": diff.to_version,
        "per_strategy_added": diff.per_strategy_added,
        "per_strategy_removed": diff.per_strategy_removed,
        # `tuple` keys in Mongo are stored as arrays — keep the shape stable.
        "per_strategy_changed": {
            k: list(v) for k, v in diff.per_strategy_changed.items()
        },
        "aggregate_ceiling_changed": (
            list(diff.aggregate_ceiling_changed)
            if diff.aggregate_ceiling_changed is not None
            else None
        ),
        "changed_by": new_bounds.changed_by,
        "changed_at": new_bounds.changed_at.isoformat(),
        "reason": new_bounds.reason,
        "logged_at": datetime.now(UTC).isoformat(),
    }
    try:
        await mongo.db[LEVERAGE_BOUNDS_AUDIT_COLLECTION].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — never block the operator PUT
        logger.warning(
            "leverage_bounds_audit.insert_failed to_version=%d error=%s",
            diff.to_version,
            exc,
        )


async def _publish_reload_event(new_bounds: LeverageBounds) -> bool:
    """Best-effort NATS publish of the reload event (AC6.c)."""
    publisher = _get_publisher()
    if publisher is None:
        logger.info(
            "leverage_bounds.publish_skipped version=%d (no publisher wired)",
            new_bounds.version,
        )
        return False

    payload = {
        "version": new_bounds.version,
        "per_strategy": new_bounds.per_strategy,
        "aggregate_ceiling": float(new_bounds.aggregate_ceiling),
        "changed_by": new_bounds.changed_by,
        "changed_at": new_bounds.changed_at.isoformat(),
        "reason": new_bounds.reason,
    }
    try:
        import json

        await publisher.publish(
            LEVERAGE_BOUNDS_NATS_SUBJECT, json.dumps(payload).encode()
        )
    except Exception as exc:  # noqa: BLE001 — never block the operator PUT
        logger.warning(
            "leverage_bounds.publish_failed version=%d error=%s",
            new_bounds.version,
            exc,
        )
        return False
    logger.info(
        "leverage_bounds.published subject=%s version=%d",
        LEVERAGE_BOUNDS_NATS_SUBJECT,
        new_bounds.version,
    )
    return True


@router.put("/api/dashboard/leverage-bounds")
async def put_leverage_bounds(req: LeverageBoundsPutRequest) -> dict[str, Any]:
    """Write a new version. Side effects: storage + audit + NATS publish."""
    mongo = _require_mongo()
    repo = LeverageBoundsRepository(mongodb_adapter=mongo)

    previous = await repo.get_latest()

    pending = LeverageBounds(
        version=1,  # overridden by repo.insert_next
        per_strategy=req.per_strategy,
        aggregate_ceiling=req.aggregate_ceiling,
        changed_by=req.changed_by,
        reason=req.reason,
    )
    inserted = await repo.insert_next(pending)

    diff = LeverageBoundsDiff.compute(previous=previous, current=inserted)
    await _persist_audit_event(mongo, diff=diff, new_bounds=inserted)
    published = await _publish_reload_event(inserted)

    return {
        "bounds": inserted.model_dump(mode="json"),
        "diff": diff.model_dump(mode="json"),
        "nats_published": published,
    }
