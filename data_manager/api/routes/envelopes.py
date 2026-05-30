"""Operator envelope-approval endpoints (#187, P4.6-AC1 / FR62).

Four endpoints mounted under ``/api/envelopes``:

  * ``GET  /pending``                            — list pending proposals (AC1.a)
  * ``POST /{change_id}/accept``                 — accept as-is (AC1.b)
  * ``POST /{change_id}/accept-with-modification`` — accept w/ overrides (AC1.c)
  * ``POST /{change_id}/reject``                 — reject w/ rationale (AC1.d)

Each resolution endpoint:

  1. Loads the pending change (404 if missing, 409 if already resolved).
  2. (Accept variants only) writes a new versioned :class:`Envelope` row
     via :class:`EnvelopeRepository.insert_next_version` — the value is
     the proposed envelope merged with any operator overrides.
  3. Marks the pending change as ``accepted`` / ``rejected`` with a
     resolution sub-document carrying ``operator_id`` + ``signed_action_id``
     (AC1.e — signed-action capture).
  4. Best-effort emits one document to the ``envelope_authorship_audit``
     Mongo collection so post-hoc analysis is unambiguous (AC1.g — FR24
     audit-trail join). Failures in the audit emit are logged but never
     block the operator request.

OpenAPI documentation is generated from these handlers' type hints +
Pydantic models with field descriptions (AC1.f).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Protocol

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, HTTPException, Path

import data_manager.api.app as api_module
from data_manager.db.repositories.envelope_repository import EnvelopeRepository
from data_manager.db.repositories.pending_envelope_change_repository import (
    PendingChangeAlreadyResolvedError,
    PendingChangeNotFoundError,
    PendingEnvelopeChangeRepository,
)
from data_manager.models.envelope import Envelope
from data_manager.models.envelope_change import (
    AcceptEnvelopeChangeRequest,
    AcceptWithModificationRequest,
    EnvelopeChangeResolution,
    PendingEnvelopeChange,
    RejectEnvelopeChangeRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION = "envelope_authorship_audit"
"""Mongo collection that captures every approve/reject action (AC1.g)."""

ENVELOPES_CHANGED_NATS_SUBJECT = "envelopes.changed"
"""Top-level NATS subject for envelope cache-bust events (P4.6-AC3, #193).

Top-level (not ``.<key>``-suffixed): a single subscriber sees every event
and filters client-side by ``strategy_or_portfolio_key``.
"""


# ── NATS publisher plumbing (parallel to leverage_bounds.set_*_publisher) ──


class _NATSPublisher(Protocol):
    async def publish(self, subject: str, payload: bytes) -> None: ...


_publisher: _NATSPublisher | None = None


def set_envelopes_changed_publisher(publisher: _NATSPublisher | None) -> None:
    """Wire the NATS publisher used by accept/reject routes (call once at boot).

    Tests inject a recording stub; passing ``None`` unwires the publisher
    (the routes then complete normally but skip the NATS emit).
    """
    global _publisher
    _publisher = publisher


def _get_publisher() -> _NATSPublisher | None:
    return _publisher


async def _publish_envelopes_changed(payload: dict[str, Any]) -> bool:
    """Best-effort NATS publish of an ``envelopes.changed`` event (AC3).

    Returns ``True`` iff a publisher was wired AND the publish call did
    not raise. All failures are logged but never propagated — the operator
    request must succeed even when NATS is down.
    """
    publisher = _get_publisher()
    if publisher is None:
        logger.info(
            "envelopes_changed.publish_skipped key=%s (no publisher wired)",
            payload.get("strategy_or_portfolio_key"),
        )
        return False
    try:
        await publisher.publish(
            ENVELOPES_CHANGED_NATS_SUBJECT, json.dumps(payload).encode()
        )
    except Exception as exc:  # noqa: BLE001 — never block the operator request
        logger.warning(
            "envelopes_changed.publish_failed key=%s error=%s",
            payload.get("strategy_or_portfolio_key"),
            exc,
        )
        return False
    logger.info(
        "envelopes_changed.published subject=%s key=%s",
        ENVELOPES_CHANGED_NATS_SUBJECT,
        payload.get("strategy_or_portfolio_key"),
    )
    return True


# ── infrastructure helpers (parallel to leverage_bounds.py) ─────────────────


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


def _pending_repo() -> PendingEnvelopeChangeRepository:
    return PendingEnvelopeChangeRepository(mongodb_adapter=_require_mongo())


def _envelope_repo() -> EnvelopeRepository:
    return EnvelopeRepository(mongodb_adapter=_require_mongo())


async def _persist_audit_event(
    mongo,  # type: ignore[no-untyped-def]
    *,
    change_id: str,
    kind: str,
    operator_id: str,
    signed_action_id: str,
    strategy_or_portfolio_key: str,
    originating_characterization_revision: str,
    before_envelope_version: int | None,
    after_envelope_version: int | None,
    proposed_envelope_value: dict[str, Any],
    accepted_envelope_value: dict[str, Any] | None,
    modification_overrides: dict[str, Any] | None,
    rejection_reason: str | None,
) -> None:
    """Best-effort emit to ``envelope_authorship_audit`` (AC1.g).

    Mirrors the shape of ``leverage_bounds_audit`` (#182): a wide flat
    document with a discriminator ``kind`` and full before/after snapshots
    so downstream queries don't need to rejoin the source collections.
    """
    doc = {
        "kind": kind,
        "change_id": change_id,
        "operator_id": operator_id,
        "signed_action_id": signed_action_id,
        "strategy_or_portfolio_key": strategy_or_portfolio_key,
        "originating_characterization_revision": originating_characterization_revision,
        "before_envelope_version": before_envelope_version,
        "after_envelope_version": after_envelope_version,
        "proposed_envelope_value": proposed_envelope_value,
        "accepted_envelope_value": accepted_envelope_value,
        "modification_overrides": modification_overrides,
        "rejection_reason": rejection_reason,
        "logged_at": datetime.now(UTC).isoformat(),
    }
    try:
        await mongo.db[ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — never block the operator request
        logger.warning(
            "envelope_authorship_audit.insert_failed change_id=%s kind=%s error=%s",
            change_id,
            kind,
            exc,
        )


def _merge_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge ``overrides`` onto ``base``; top-level keys in
    ``overrides`` replace those in ``base``. Documented in
    ``AcceptWithModificationRequest.modification_overrides``."""
    merged = dict(base)
    merged.update(overrides)
    return merged


async def _load_pending_or_raise(
    change_id: str,
    pending_repo: PendingEnvelopeChangeRepository,
) -> PendingEnvelopeChange:
    """Load a pending change; 404 if missing, 409 if already resolved."""
    change = await pending_repo.get(change_id)
    if change is None:
        raise HTTPException(
            status_code=404,
            detail={
                "title": "pending envelope change not found",
                "detail": f"no document with change_id={change_id!r}",
            },
        )
    if change.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={
                "title": "pending envelope change already resolved",
                "detail": f"change_id={change_id!r} status={change.status}",
            },
        )
    return change


async def _write_envelope_from_acceptance(
    change: PendingEnvelopeChange,
    *,
    value: dict[str, Any],
    operator_id: str,
    signed_action_id: str,
    envelope_repo: EnvelopeRepository,
) -> Envelope:
    """Persist the accepted envelope as a new versioned Envelope row (P4.6-AC2 link)."""
    candidate = Envelope(
        envelope_id=f"{change.strategy_or_portfolio_key}:vNEW",  # overridden by repo
        version=1,  # overridden by repo
        strategy_or_portfolio_key=change.strategy_or_portfolio_key,
        value=value,
        source="operator_approved",
        originating_characterization_revision=change.originating_characterization_revision,
        operator_id=operator_id,
        signed_action_id=signed_action_id,
    )
    return await envelope_repo.insert_next_version(candidate)


# ── routes ──────────────────────────────────────────────────────────────────


@router.get("/api/envelopes/pending")
async def list_pending_envelope_changes(limit: int = 200) -> dict[str, Any]:
    """List all pending envelope changes awaiting operator review (AC1.a).

    Returns newest-first. ``limit`` caps the response (default 200) to
    bound the worst case.
    """
    repo = _pending_repo()
    pending = await repo.list_pending(limit=limit)
    return {
        "pending": [change.model_dump(mode="json") for change in pending],
        "count": len(pending),
    }


@router.get("/api/envelopes/active/{key}")
async def get_active_envelope(
    key: str = Path(..., min_length=1, description="strategy_or_portfolio_key"),
) -> dict[str, Any]:
    """Return the active (highest-version) envelope for ``key`` (#200).

    Wraps :meth:`EnvelopeRepository.get_full_active_envelope` so out-of-process
    consumers (cio envelope-fetch helper from #154, tradeengine FR30 wiring
    from #421) can read the active envelope without sharing the Mongo
    connection.

    The repository returns the highest ``version`` regardless of ``source``
    (AC3 of #154 — operator-approved precedence by version, not by source):
    if an operator-approved ``v=4`` row exists and a characterization ``v=5``
    arrives afterward, the read returns ``v=5``. That precedence-by-version
    is intentional and is asserted by the cross-repo helper tests.

    Responses:
      * ``200`` — full ``Envelope`` document as JSON (envelope_id, version,
        strategy_or_portfolio_key, value, source, originating_characterization_revision,
        operator_id, created_at, signed_action_id).
      * ``404`` — no envelope exists for the given key.
      * ``503`` — MongoDB is unavailable (data-manager in limited mode).
    """
    repo = _envelope_repo()
    envelope = await repo.get_full_active_envelope(key)
    if envelope is None:
        raise HTTPException(
            status_code=404,
            detail={
                "title": "Envelope not found",
                "detail": f"No envelope exists for strategy_or_portfolio_key={key!r}",
            },
        )
    return envelope.model_dump(mode="json")


@router.post("/api/envelopes/{change_id}/accept")
async def accept_envelope_change(
    req: AcceptEnvelopeChangeRequest,
    change_id: str = Path(..., min_length=1),
) -> dict[str, Any]:
    """Accept a pending change as-is (AC1.b).

    Side effects:
      1. Writes a new ``Envelope`` row at the next version for the key,
         with ``value`` = proposed envelope value, ``source='operator_approved'``.
      2. Flips the pending change to ``status='accepted'`` with the
         resolution sub-document (operator + signed-action).
      3. Emits an ``envelope_authorship_audit`` document (AC1.g).
    """
    mongo = _require_mongo()
    pending_repo = PendingEnvelopeChangeRepository(mongodb_adapter=mongo)
    envelope_repo = EnvelopeRepository(mongodb_adapter=mongo)

    change = await _load_pending_or_raise(change_id, pending_repo)
    inserted = await _write_envelope_from_acceptance(
        change,
        value=change.proposed_envelope_value,
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        envelope_repo=envelope_repo,
    )
    resolution = EnvelopeChangeResolution(
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        modification_overrides=None,
        rejection_reason=None,
    )

    try:
        resolved = await pending_repo.resolve(
            change_id, target_status="accepted", resolution=resolution
        )
    except PendingChangeNotFoundError as exc:  # pragma: no cover — caught earlier
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PendingChangeAlreadyResolvedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "title": "race: pending change resolved by another operator",
                "detail": str(exc),
            },
        ) from exc

    await _persist_audit_event(
        mongo,
        change_id=change_id,
        kind="envelope_change_accepted",
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        strategy_or_portfolio_key=change.strategy_or_portfolio_key,
        originating_characterization_revision=change.originating_characterization_revision,
        before_envelope_version=change.current_envelope_version,
        after_envelope_version=inserted.version,
        proposed_envelope_value=change.proposed_envelope_value,
        accepted_envelope_value=inserted.value,
        modification_overrides=None,
        rejection_reason=None,
    )

    nats_published = await _publish_envelopes_changed(
        {
            "strategy_or_portfolio_key": change.strategy_or_portfolio_key,
            "new_version": inserted.version,
            "source": inserted.source,
            "signed_action_id": req.signed_action_id,
            "originating_characterization_revision": change.originating_characterization_revision,
            "accepted_at": datetime.now(UTC).isoformat(),
        }
    )

    return {
        "change": resolved.model_dump(mode="json"),
        "envelope": inserted.model_dump(mode="json"),
        "nats_published": nats_published,
    }


@router.post("/api/envelopes/{change_id}/accept-with-modification")
async def accept_envelope_change_with_modification(
    req: AcceptWithModificationRequest,
    change_id: str = Path(..., min_length=1),
) -> dict[str, Any]:
    """Accept a pending change with operator overrides (AC1.c).

    ``modification_overrides`` is shallow-merged onto
    ``proposed_envelope_value`` (top-level keys win) and the result is
    persisted as the new ``Envelope.value``.
    """
    mongo = _require_mongo()
    pending_repo = PendingEnvelopeChangeRepository(mongodb_adapter=mongo)
    envelope_repo = EnvelopeRepository(mongodb_adapter=mongo)

    change = await _load_pending_or_raise(change_id, pending_repo)
    merged_value = _merge_overrides(
        change.proposed_envelope_value, req.modification_overrides
    )
    inserted = await _write_envelope_from_acceptance(
        change,
        value=merged_value,
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        envelope_repo=envelope_repo,
    )
    resolution = EnvelopeChangeResolution(
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        modification_overrides=req.modification_overrides,
        rejection_reason=None,
    )

    try:
        resolved = await pending_repo.resolve(
            change_id, target_status="accepted", resolution=resolution
        )
    except PendingChangeAlreadyResolvedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "title": "race: pending change resolved by another operator",
                "detail": str(exc),
            },
        ) from exc

    await _persist_audit_event(
        mongo,
        change_id=change_id,
        kind="envelope_change_accepted_with_modification",
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        strategy_or_portfolio_key=change.strategy_or_portfolio_key,
        originating_characterization_revision=change.originating_characterization_revision,
        before_envelope_version=change.current_envelope_version,
        after_envelope_version=inserted.version,
        proposed_envelope_value=change.proposed_envelope_value,
        accepted_envelope_value=inserted.value,
        modification_overrides=req.modification_overrides,
        rejection_reason=None,
    )

    nats_published = await _publish_envelopes_changed(
        {
            "strategy_or_portfolio_key": change.strategy_or_portfolio_key,
            "new_version": inserted.version,
            "source": inserted.source,
            "signed_action_id": req.signed_action_id,
            "originating_characterization_revision": change.originating_characterization_revision,
            "accepted_at": datetime.now(UTC).isoformat(),
        }
    )

    return {
        "change": resolved.model_dump(mode="json"),
        "envelope": inserted.model_dump(mode="json"),
        "nats_published": nats_published,
    }


@router.post("/api/envelopes/{change_id}/reject")
async def reject_envelope_change(
    req: RejectEnvelopeChangeRequest,
    change_id: str = Path(..., min_length=1),
) -> dict[str, Any]:
    """Reject a pending change with operator rationale (AC1.d).

    No new ``Envelope`` row is written — the active envelope (if any)
    remains in force.
    """
    mongo = _require_mongo()
    pending_repo = PendingEnvelopeChangeRepository(mongodb_adapter=mongo)

    change = await _load_pending_or_raise(change_id, pending_repo)
    resolution = EnvelopeChangeResolution(
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        modification_overrides=None,
        rejection_reason=req.rejection_reason,
    )

    try:
        resolved = await pending_repo.resolve(
            change_id, target_status="rejected", resolution=resolution
        )
    except PendingChangeAlreadyResolvedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "title": "race: pending change resolved by another operator",
                "detail": str(exc),
            },
        ) from exc

    await _persist_audit_event(
        mongo,
        change_id=change_id,
        kind="envelope_change_rejected",
        operator_id=req.operator_id,
        signed_action_id=req.signed_action_id,
        strategy_or_portfolio_key=change.strategy_or_portfolio_key,
        originating_characterization_revision=change.originating_characterization_revision,
        before_envelope_version=change.current_envelope_version,
        after_envelope_version=None,
        proposed_envelope_value=change.proposed_envelope_value,
        accepted_envelope_value=None,
        modification_overrides=None,
        rejection_reason=req.rejection_reason,
    )

    nats_published = await _publish_envelopes_changed(
        {
            "strategy_or_portfolio_key": change.strategy_or_portfolio_key,
            "status": "rejected",
            "change_id": change_id,
            "operator_id": req.operator_id,
            "signed_action_id": req.signed_action_id,
        }
    )

    return {
        "change": resolved.model_dump(mode="json"),
        "nats_published": nats_published,
    }
