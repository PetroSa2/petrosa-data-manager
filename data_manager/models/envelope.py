"""Versioned drawdown / portfolio envelopes (#188, P4.6-AC2 / FR62).

Each accepted envelope (either characterization-derived or operator-approved
via 692.1's signed-action approval API) writes a new monotonically-versioned
document, keyed per ``strategy_or_portfolio_key``, to the ``envelopes``
collection. Readers always fetch the latest version for a given key via
:meth:`data_manager.db.repositories.envelope_repository.EnvelopeRepository.get_active_envelope`.

Older versions are never mutated — operators (and the breach-event audit
join in 692.6) can trace exactly which envelope was in force at any point
in time, and the audit-trail carries the diff between versions.

## Key shape decision (#188 AC2.a)

The AC names the partitioning column ``strategy_or_portfolio_key`` without
declaring whether it's a flat string or a structured composite. This model
adopts **flat string** for parity with existing FR61 patterns
(``LeverageBounds.per_strategy: dict[str, int]`` keys are flat strings).

Recommended forms:

* For a per-strategy envelope: ``"strategy:<strategy_id>"`` (e.g. ``"strategy:btc_momentum_v3"``).
* For a portfolio-level envelope: ``"portfolio:<portfolio_id>"`` (e.g. ``"portfolio:operator-1"``).

The repository does NOT enforce a prefix scheme — callers are responsible
for choosing a stable, non-overlapping namespace. The compound index
``(strategy_or_portfolio_key, version)`` makes lookups O(log n) regardless
of the chosen scheme.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field

EnvelopeSource = Literal["characterization", "operator_approved"]


class Envelope(BaseModel):
    """A versioned snapshot of an active envelope (per AC2.a, P4.6-AC2 / FR62).

    The Mongo ``_id`` of the persisted document is the composite
    ``"<strategy_or_portfolio_key>:v<version>"``. The unique-id constraint
    enforces AC2.b (strict monotonicity per key) — two writers racing for
    the same ``v<n>`` cannot both succeed; the losing writer retries with
    ``v<n+1>``. See :class:`EnvelopeRepository` for the retry loop.
    """

    envelope_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable identity for an envelope authored event. Suggested form: "
            "``<strategy_or_portfolio_key>:v<version>``; the repository will "
            "stamp it during insert."
        ),
    )
    version: int = Field(..., ge=1, description="Monotonic version (1-based, per-key).")
    strategy_or_portfolio_key: str = Field(
        ...,
        min_length=1,
        description=(
            "Partition column. Flat string; see module docstring for the "
            "recommended ``strategy:<id>`` / ``portfolio:<id>`` namespace."
        ),
    )
    value: dict[str, Any] = Field(
        ...,
        description=(
            "Schema-free envelope payload (caps, allowable drawdown, etc.). "
            "Validated by consumers per their own contract; the store does "
            "not assume a shape here."
        ),
    )
    source: EnvelopeSource = Field(
        ...,
        description="Whether this envelope was characterization-derived or operator-approved.",
    )
    originating_characterization_revision: str | None = Field(
        default=None,
        description=(
            "FR53 link — when ``source=='characterization'``, the revision id "
            "of the characterization that produced this envelope. None for "
            "``source=='operator_approved'`` (operator decision is the "
            "originator of record)."
        ),
    )
    operator_id: str | None = Field(
        default=None,
        description=(
            "Operator identity for ``source=='operator_approved'`` envelopes. "
            "None for characterization-derived ones."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this envelope version was written.",
    )
    signed_action_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Signed-action audit id (692.1 approval-API or the "
            "characterization pipeline's signed-action id) that produced "
            "this envelope. Joins this row to the FR12 audit trail."
        ),
    )

    def doc_id(self) -> str:
        """Mongo ``_id`` form (``"<key>:v<n>"``) used by the repository."""
        return f"{self.strategy_or_portfolio_key}:v{self.version}"
