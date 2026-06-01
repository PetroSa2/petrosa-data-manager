"""``GET /api/breaches/{id}`` route — P4.6-AC6.c / FR62 / #194.

Returns the persisted drawdown breach AND a hydrated snapshot of the
envelope it was measured against, if known. Legacy breaches (predating
AC6.a #422) return ``envelope=null`` per AC6.c.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Path

import data_manager.api.app as api_module
from data_manager.db.repositories.drawdown_breach_repository import (
    DrawdownBreachRepository,
)
from data_manager.db.repositories.envelope_repository import EnvelopeRepository

logger = logging.getLogger(__name__)
router = APIRouter()


def _raise_problem(*, status: int, title: str, detail: str) -> None:
    raise HTTPException(
        status_code=status,
        detail={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


def _require_mongo():
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        _raise_problem(
            status=503,
            title="database_unavailable",
            detail="MongoDB is not configured or not yet connected.",
        )
    return api_module.db_manager.mongodb_adapter


@router.get("/{breach_id}")
async def get_breach(
    breach_id: str = Path(..., min_length=1, description="The breach_id (Mongo _id)."),
) -> dict[str, Any]:
    """Return ``{breach, envelope}`` for one drawdown breach.

    ``envelope`` is hydrated via ``EnvelopeRepository.get_version`` keyed
    by the breach's ``envelope_version``. Returns ``null`` for legacy
    breaches whose ``envelope_version`` is null (AC6.c).
    """
    mongo = _require_mongo()
    breach_repo = DrawdownBreachRepository(mongodb_adapter=mongo)
    envelope_repo = EnvelopeRepository(mongodb_adapter=mongo)

    try:
        breach = await breach_repo.get_by_id(breach_id)
    except Exception as exc:
        logger.error(
            "breach_lookup_failed",
            extra={"breach_id": breach_id, "error": str(exc)},
            exc_info=True,
        )
        _raise_problem(
            status=500,
            title="breach_lookup_failed",
            detail="Failed to query drawdown_breaches collection.",
        )

    if breach is None:
        _raise_problem(
            status=404,
            title="breach_not_found",
            detail=f"No drawdown breach with breach_id={breach_id!r}.",
        )

    envelope_payload = None
    if breach.envelope_version is not None:
        try:
            key = f"strategy:{breach.strategy_id}"
            envelope = await envelope_repo.get_version(key, breach.envelope_version)
            if envelope is not None:
                envelope_payload = envelope.model_dump(mode="json")
        except Exception as exc:
            # Envelope hydration is best-effort — the breach itself is the
            # authoritative record. Log but don't fail the request.
            logger.warning(
                "breach_envelope_hydration_failed",
                extra={
                    "breach_id": breach_id,
                    "envelope_version": breach.envelope_version,
                    "error": str(exc),
                },
            )

    return {
        "breach": breach.model_dump(mode="json"),
        "envelope": envelope_payload,
    }
