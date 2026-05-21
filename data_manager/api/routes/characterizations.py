"""Characterization API endpoints (P3.2, petrosa_k8s#599)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.db.repositories.characterization_repository import (
    CharacterizationRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _repo() -> CharacterizationRepository | None:
    """Build a CharacterizationRepository or return None if DB is unavailable."""
    if not api_module.db_manager:
        return None
    if not getattr(api_module.db_manager, "mongodb_adapter", None):
        return None
    return CharacterizationRepository(
        mysql_adapter=api_module.db_manager.mysql_adapter,
        mongodb_adapter=api_module.db_manager.mongodb_adapter,
    )


@router.get("/characterizations")
async def get_characterization(
    strategy_id: str = Query(..., description="Strategy identifier"),
    version: str | None = Query(
        None,
        description=(
            "Exact strategy version to fetch. When omitted, returns the most "
            "recent characterization for the strategy."
        ),
    ),
) -> dict:
    """Return one characterization for the given strategy.

    Lookup semantics:
      - With ``version``: exact (strategy_id, version) row, or 404.
      - Without ``version``: most recently persisted row for the strategy.

    The endpoint never returns a list; callers wanting multiple versions
    should call this endpoint per version, or extend the API in a
    follow-up ticket.
    """
    repo = _repo()
    if repo is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        artifact = (
            await repo.get_version(strategy_id, version)
            if version
            else await repo.get_latest(strategy_id)
        )
    except Exception as exc:
        logger.error(
            "characterizations: lookup failed for %s/%s: %s",
            strategy_id,
            version,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="lookup failed") from exc

    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no characterization for strategy_id={strategy_id}"
                + (f", version={version}" if version else "")
            ),
        )

    return artifact.model_dump(mode="json")
