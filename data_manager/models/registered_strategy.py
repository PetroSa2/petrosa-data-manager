"""Strategy-registry models (#195, FR54).

The `strategy_registry` Mongo collection holds operator-submitted strategy
definitions. Each `RegisteredStrategy` document is the persisted form of one
strategy submission via the `bot-ta strategy submit` CLI in
`petrosa-bot-ta-analysis` (#255 — CLI side, blocked by this leaf).

**Important — no execution here.** The `code` field is persisted verbatim
but never imported, compiled, exec'd, or otherwise evaluated by the
data-manager. Code execution is a separate sandboxing concern handled
CLI-side in #255 AC3 after security/threat-model sign-off.

## Status lifecycle

- `candidate` — submitted, not yet validated/promoted (initial state on POST)
- `accepted` — promoted to a runnable strategy by a downstream review process
- `rejected` — declined; remains in the registry for audit

This leaf only writes documents in `candidate` state. Subsequent state
transitions are the concern of a follow-up ticket (a status-update endpoint
is intentionally out of scope here).
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

StrategyStatus = Literal["candidate", "accepted", "rejected"]


class RegisteredStrategy(BaseModel):
    """A persisted strategy submission in the `strategy_registry` collection.

    The Mongo `_id` is set to `strategy_id` so the unique-id constraint
    enforces the per-id uniqueness contract (AC bullet "unique on
    strategy_id"): a duplicate POST surfaces as a `DuplicateKeyError` and
    the route returns 409.
    """

    strategy_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "Stable, caller-supplied identity for the strategy. Used as the "
            "Mongo `_id` of the persisted document; unique within the "
            "registry."
        ),
    )
    code: str = Field(
        ...,
        min_length=1,
        description=(
            "Verbatim strategy source code. Persisted as-is. NEVER executed, "
            "imported, or compiled by data-manager. Execution is a separate "
            "sandboxed concern (see #255 AC3 / module docstring)."
        ),
    )
    parameter_set: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form parameter dictionary for the strategy. Validated by "
            "the strategy implementation downstream; the registry does not "
            "assume a schema here."
        ),
    )
    symbol_scope: list[str] = Field(
        default_factory=list,
        description=(
            "Symbols the strategy is intended to trade (e.g. ['BTCUSDT', "
            "'ETHUSDT']). May be empty when the strategy is symbol-agnostic."
        ),
    )
    submitted_by: str = Field(
        ...,
        min_length=1,
        description="Operator (or service) identity that submitted this strategy.",
    )
    signed_action_id: str = Field(
        ...,
        min_length=1,
        description=(
            "FR12 signed-action correlation id, matching the audit-trail "
            "envelope on the submitter side. Required."
        ),
    )
    status: StrategyStatus = Field(
        default="candidate",
        description=(
            "Lifecycle status. POST always lands in `candidate`; promotion "
            "to `accepted`/`rejected` is out of scope of this leaf."
        ),
    )
    registered_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Server-stamped registration timestamp (UTC).",
    )


class CreateStrategyRequest(BaseModel):
    """Body schema for `POST /api/strategies`.

    Mirrors the AC contract verbatim: `{strategy_id, code, parameter_set,
    symbol_scope, submitted_by, signed_action_id}`. `status` and
    `registered_at` are server-controlled and not accepted on the request.
    """

    strategy_id: str = Field(..., min_length=1, max_length=256)
    code: str = Field(..., min_length=1)
    parameter_set: dict[str, Any] = Field(default_factory=dict)
    symbol_scope: list[str] = Field(default_factory=list)
    submitted_by: str = Field(..., min_length=1)
    signed_action_id: str = Field(..., min_length=1)
