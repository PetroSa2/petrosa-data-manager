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
    strategy_revision_id: str | None = Query(
        None,
        description=(
            "FR53 / P3.4 — filter by content-addressable strategy revision. "
            "When set, returns the most recent characterization for "
            "(strategy_id, strategy_revision_id) or 404 (stale revision)."
        ),
    ),
) -> dict:
    """Return one characterization for the given strategy.

    Lookup semantics (first match wins):
      - With ``strategy_revision_id``: most recent characterization whose
        ``strategy_revision_id`` matches, or 404 (FR53 / P3.4 — consumers
        use this to refuse intents against stale revisions).
      - With ``version`` (and no revision filter): exact
        (strategy_id, version) row, or 404.
      - Otherwise: most recently persisted row for the strategy.

    The endpoint never returns a list; callers wanting multiple versions
    should call this endpoint per version, or extend the API in a
    follow-up ticket.
    """
    repo = _repo()
    if repo is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        if strategy_revision_id is not None:
            artifact = await repo.get_by_strategy_revision(
                strategy_id, strategy_revision_id
            )
        elif version:
            artifact = await repo.get_version(strategy_id, version)
        else:
            artifact = await repo.get_latest(strategy_id)
    except Exception as exc:
        logger.error(
            "characterizations: lookup failed for %s/%s/rev=%s: %s",
            strategy_id,
            version,
            strategy_revision_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="lookup failed") from exc

    if artifact is None:
        detail = f"no characterization for strategy_id={strategy_id}"
        if version:
            detail += f", version={version}"
        if strategy_revision_id:
            detail += f", strategy_revision_id={strategy_revision_id}"
        raise HTTPException(status_code=404, detail=detail)

    return artifact.model_dump(mode="json")
