"""Operator-configurable leverage bounds with versioning (#182, P1.5-AC6 / FR61).

Each PUT through ``/api/dashboard/leverage-bounds`` writes a new
monotonically-versioned document to the ``leverage_bounds`` collection.
Readers always fetch the latest by sorting ``_id`` descending and
expecting the documents to be keyed as ``"v<n>"`` strings where ``n``
is a 1-based integer. Older versions are never mutated — operators can
trace exactly what the bounds were at any point in time, and the audit
trail (FR12) carries the diff between versions.
"""

from __future__ import annotations

from datetime import datetime

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field, NonNegativeFloat


class LeverageBounds(BaseModel):
    """A versioned snapshot of the operator-configured leverage bounds.

    ``_id`` is rendered as ``v<n>`` for monotonic version numbering so the
    Mongo natural sort order matches version order via the helper in
    :mod:`data_manager.db.repositories.leverage_bounds_repository`.
    """

    version: int = Field(..., ge=1, description="Monotonic version (1-based)")
    per_strategy: dict[str, int] = Field(
        default_factory=dict,
        description="Per-strategy_id leverage cap. Missing keys fall back to env-var defaults.",
    )
    aggregate_ceiling: NonNegativeFloat = Field(
        ...,
        description="Aggregate Σ(position_size × leverage) / equity ceiling (FR61 AC5).",
    )
    changed_by: str = Field(
        ..., min_length=1, description="Operator identity that authored this version"
    )
    changed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this version was written",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Operator-supplied rationale (audit-trail diff line).",
    )

    def doc_id(self) -> str:
        """Mongo ``_id`` form (``"v<n>"``) used by the repository."""
        return f"v{self.version}"


class LeverageBoundsPutRequest(BaseModel):
    """Request body for the PUT endpoint — no version field; the server assigns it."""

    per_strategy: dict[str, int] = Field(default_factory=dict)
    aggregate_ceiling: NonNegativeFloat
    changed_by: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=500)


class LeverageBoundsDiff(BaseModel):
    """Structured diff between two versions, used by the audit trail (AC6.d)."""

    from_version: int | None = Field(
        None, description="None when this is the first version ever written"
    )
    to_version: int
    per_strategy_added: dict[str, int] = Field(default_factory=dict)
    per_strategy_removed: dict[str, int] = Field(default_factory=dict)
    per_strategy_changed: dict[str, tuple[int, int]] = Field(
        default_factory=dict,
        description="strategy_id -> (old_cap, new_cap)",
    )
    aggregate_ceiling_changed: tuple[float, float] | None = Field(
        None, description="(old, new); None when unchanged"
    )

    @classmethod
    def compute(
        cls,
        previous: LeverageBounds | None,
        current: LeverageBounds,
    ) -> LeverageBoundsDiff:
        """Diff (added/removed/changed) between previous and current bounds.

        When ``previous`` is None (first version ever written), every entry
        in ``current.per_strategy`` is reported as added and the aggregate
        ceiling is reported as a change from None.
        """
        prev_per = previous.per_strategy if previous else {}
        cur_per = current.per_strategy

        added = {k: v for k, v in cur_per.items() if k not in prev_per}
        removed = {k: v for k, v in prev_per.items() if k not in cur_per}
        changed = {
            k: (prev_per[k], cur_per[k])
            for k in cur_per
            if k in prev_per and prev_per[k] != cur_per[k]
        }

        agg_changed: tuple[float, float] | None = None
        if previous is None:
            agg_changed = (0.0, float(current.aggregate_ceiling))
        elif float(previous.aggregate_ceiling) != float(current.aggregate_ceiling):
            agg_changed = (
                float(previous.aggregate_ceiling),
                float(current.aggregate_ceiling),
            )

        return cls(
            from_version=previous.version if previous else None,
            to_version=current.version,
            per_strategy_added=added,
            per_strategy_removed=removed,
            per_strategy_changed=changed,
            aggregate_ceiling_changed=agg_changed,
        )

    def is_noop(self) -> bool:
        """True when nothing actually changed between the two versions."""
        return (
            not self.per_strategy_added
            and not self.per_strategy_removed
            and not self.per_strategy_changed
            and self.aggregate_ceiling_changed is None
        )
