"""Pending envelope-change proposals awaiting operator approval (#187, P4.6-AC1 / FR62).

A ``PendingEnvelopeChange`` is the consumer-half of the FR62 authorship
workflow. Producers (e.g. the characterization pipeline; see EPIC #692
sibling ACs) write one document per detected divergence between the
characterization-derived envelope and the active operator-approved
envelope for a given ``strategy_or_portfolio_key``. Operators then
review-and-resolve via the 4 endpoints in
``data_manager.api.routes.envelopes``:

* ``GET  /api/envelopes/pending``                          — AC1.a
* ``POST /api/envelopes/{change_id}/accept``               — AC1.b
* ``POST /api/envelopes/{change_id}/accept-with-modification``  — AC1.c
* ``POST /api/envelopes/{change_id}/reject``               — AC1.d

Resolution is captured in-place (status transitions ``pending`` →
``accepted`` | ``rejected``) so the change history is self-contained;
the resulting Envelope row is written to the
:class:`data_manager.models.envelope.Envelope` versioned store
(#188 / P4.6-AC2) by the route handler in the same request.

Append-only with respect to ``change_id``: once resolved a document is
not deleted, only marked. The operator history pane reads pending +
recently resolved in one window.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field, model_validator

PendingChangeStatus = Literal["pending", "accepted", "rejected"]


class EnvelopeChangeResolution(BaseModel):
    """Sub-document capturing how a pending change was closed (AC1.b/c/d/e/g).

    Populated only when ``status != 'pending'``. Carries the operator
    identity + signed-action audit id required by AC1.e, plus
    branch-specific fields:

    * ``accept-as-is``:   ``modification_overrides=None``, ``rejection_reason=None``
    * ``accept-with-modification``:  ``modification_overrides`` set
    * ``reject``:  ``rejection_reason`` set
    """

    operator_id: str = Field(
        ...,
        min_length=1,
        description="Identity of the operator that resolved this change (AC1.e).",
    )
    signed_action_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Audit identifier for the signed action that authorized the "
            "resolution (AC1.e). Joined to the FR12 audit trail and to "
            "the resulting :class:`Envelope.signed_action_id`."
        ),
    )
    resolved_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp the resolution was committed.",
    )
    modification_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "For ``accept-with-modification``: the operator-supplied "
            "overrides merged into the proposed envelope before persisting. "
            "Null for ``accept`` (as-is) and ``reject``."
        ),
    )
    rejection_reason: str | None = Field(
        default=None,
        description=(
            "For ``reject``: operator-supplied rationale (required, "
            "AC1.d). Null for both accept variants."
        ),
    )

    @model_validator(mode="after")
    def _validate_branch_fields(self) -> EnvelopeChangeResolution:
        if (
            self.modification_overrides is not None
            and self.rejection_reason is not None
        ):
            raise ValueError(
                "modification_overrides and rejection_reason are mutually exclusive"
            )
        return self


class PendingEnvelopeChange(BaseModel):
    """A proposed envelope change awaiting operator review.

    The Mongo ``_id`` of the persisted document is ``change_id`` (UUID),
    so duplicate writes are caller-controlled and idempotent.
    """

    change_id: str = Field(
        ...,
        min_length=1,
        description="UUID identifier for the proposal (becomes the Mongo _id).",
    )
    strategy_or_portfolio_key: str = Field(
        ...,
        min_length=1,
        description=(
            "Partition column matching the active Envelope's. Same flat "
            "string convention as :mod:`data_manager.models.envelope`."
        ),
    )
    proposed_envelope_value: dict[str, Any] = Field(
        ...,
        description=(
            "The new envelope value the producer is proposing. "
            "Schema-free; consumers validate their own contracts."
        ),
    )
    current_envelope_version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Version of the active Envelope this change supersedes "
            "(None if no envelope yet exists for the key — first-ever "
            "characterization-derived envelope)."
        ),
    )
    current_envelope_value: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Snapshot of the active envelope value at the time the "
            "proposal was filed (advisory only — operator UI reads this "
            "to render a diff)."
        ),
    )
    diverging_pct_per_strategy: dict[str, float] | None = Field(
        default=None,
        description=(
            "Per-strategy divergence percentage (producer-computed). "
            "Advisory for UI sorting / highlight. Null if not provided."
        ),
    )
    originating_characterization_revision: str = Field(
        ...,
        min_length=1,
        description=(
            "FR53 cross-link: the characterization revision id that "
            "produced this proposal. Required — every pending change "
            "must trace back to a characterization revision."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the producer filed the proposal.",
    )
    status: PendingChangeStatus = Field(
        default="pending",
        description="Lifecycle state. Transitions are one-way out of 'pending'.",
    )
    resolution: EnvelopeChangeResolution | None = Field(
        default=None,
        description="Populated when status != 'pending'.",
    )

    @model_validator(mode="after")
    def _resolution_matches_status(self) -> PendingEnvelopeChange:
        if self.status == "pending" and self.resolution is not None:
            raise ValueError("pending changes must not carry a resolution")
        if self.status != "pending" and self.resolution is None:
            raise ValueError("resolved changes must carry a resolution sub-document")
        if self.status == "rejected" and self.resolution is not None:
            if not self.resolution.rejection_reason:
                raise ValueError("rejected changes require resolution.rejection_reason")
        return self


# ── Request DTOs for the route handlers ─────────────────────────────────────


class AcceptEnvelopeChangeRequest(BaseModel):
    """Body for ``POST /api/envelopes/{change_id}/accept`` (AC1.b)."""

    operator_id: str = Field(..., min_length=1)
    signed_action_id: str = Field(..., min_length=1)


class AcceptWithModificationRequest(BaseModel):
    """Body for ``POST /api/envelopes/{change_id}/accept-with-modification`` (AC1.c)."""

    operator_id: str = Field(..., min_length=1)
    signed_action_id: str = Field(..., min_length=1)
    modification_overrides: dict[str, Any] = Field(
        ...,
        description=(
            "Operator-supplied overrides merged into the proposed envelope. "
            "Top-level keys in this dict replace the corresponding keys in "
            "``proposed_envelope_value`` (shallow merge)."
        ),
    )


class RejectEnvelopeChangeRequest(BaseModel):
    """Body for ``POST /api/envelopes/{change_id}/reject`` (AC1.d)."""

    operator_id: str = Field(..., min_length=1)
    signed_action_id: str = Field(..., min_length=1)
    rejection_reason: str = Field(
        ...,
        min_length=1,
        description="Operator-supplied rationale; required by AC1.d.",
    )
