"""Alert event model persisted to the `alerts` audit-trail collection (#183).

Implements **AC4 — audit-trail persistence** of the alert spine: every event
arriving on `alerts.>` is materialised into a typed `AlertEvent`, deduped by a
deterministic `_id = f"{category}::{dedupe_key}::{timestamp_iso}"`, and stored
with its delivery state so re-deliveries replace in-place.

The dispatcher (`data_manager/services/alert_dispatcher.py`) owns the
subscription + delivery loop; this module is shape only — no I/O.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field


class AlertSeverity(str, Enum):
    """Alert severity. Maps 1:1 to Grafana / PagerDuty conventions."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertDeliveryState(str, Enum):
    """Delivery lifecycle state for an alert event (AC3)."""

    # AC3 — initial state when the event lands in the collection before
    # dispatch is attempted. Distinct from `retry` so the operator can tell
    # "in flight" from "had a setback."
    PENDING = "pending"
    # AC3 — bounded retry exhausted OR webhook unreachable; left in the
    # audit trail for post-mortem inspection. NOT re-attempted automatically.
    FAILED = "failed"
    # AC3 — successful delivery to the configured Grafana Cloud webhook.
    DELIVERED = "delivered"
    # AC3 — delivery skipped because no webhook URL is configured. The event
    # still lives in the audit trail; production wire-up flips the state to
    # `delivered` on next dispatch without code changes.
    DELIVERED_MOCK = "delivered_mock"
    # AC3 — between attempts within the bounded-retry envelope (transient).
    # The dispatcher walks back to this state after each backoff sleep.
    RETRY = "retry"


class DeliveryAttempt(BaseModel):
    """One entry in `delivery_attempts[]` (AC4)."""

    attempt: int = Field(..., ge=1, description="1-based attempt counter")
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state: AlertDeliveryState
    http_status: int | None = Field(
        default=None,
        description="Webhook HTTP status code, or None when delivery was mocked.",
    )
    error: str | None = Field(
        default=None,
        description="Operator-readable error string (truncated to 500 chars).",
    )


class AlertEvent(BaseModel):
    """An event published on `alerts.>` (AC4).

    The `_id` for the MongoDB document is computed externally as
    `f"{category}::{dedupe_key}::{timestamp_iso}"` so re-deliveries from the
    NATS replay path replace the same row in-place (AC3 — at-least-once
    delivery + dedup).
    """

    category: str = Field(
        ...,
        min_length=1,
        description=(
            "Hierarchical alert category — derived from the NATS subject "
            "(e.g. `position.reconciliation.mismatch`, `backup_failed`). "
            "Used as the rate-limit bucket key (AC5)."
        ),
    )
    severity: AlertSeverity = AlertSeverity.WARNING
    subsystem: str | None = Field(
        default=None,
        description=(
            "Producing subsystem (e.g. `tradeengine`, `cio`). Optional — some "
            "categories carry `strategy_id` instead."
        ),
    )
    strategy_id: str | None = Field(
        default=None,
        description="Producing strategy when the alert is strategy-scoped.",
    )
    message: str = Field(
        default="",
        description="Operator-readable summary. Empty allowed for terse alerts.",
    )
    decision_id: str | None = Field(
        default=None,
        description=(
            "Cross-service decision-id propagation key (per FR27). When the "
            "alert is tied to a specific CIO decision the back-link lives "
            "here so the dashboard can pivot decision ↔ alert."
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Full original NATS body (post-trace-strip).",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Event timestamp from the publisher.",
    )
    # AC4 — delivery state machine.
    delivery_state: AlertDeliveryState = AlertDeliveryState.PENDING
    delivery_attempts: list[DeliveryAttempt] = Field(
        default_factory=list,
        description="Append-only delivery audit trail (AC4).",
    )
    # AC3 dedup key — falls back to `decision_id` when present, else to a
    # deterministic projection of the subsystem+message (so two identical
    # alerts within the timestamp window collapse to one row).
    dedupe_key: str = Field(
        ...,
        description=(
            "Stable dedup key. Producer SHOULD pass `decision_id` or a "
            "natural unique id for the event (e.g. `position_id` for "
            "reconciliation mismatches)."
        ),
    )
    # AC5 — summary-alert rollup back-link. When this row is itself a
    # `alerts.summary.<category>` event, `summarized_ids` lists the
    # suppressed `_id`s this summary stands in for.
    summarized_ids: list[str] = Field(
        default_factory=list,
        description=(
            "When `category` starts with `summary.`, the suppressed event IDs "
            "this summary references. Empty for non-summary alerts."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Persistence timestamp (subscriber-side, NOT publisher).",
    )

    def make_id(self) -> str:
        """Compute the deterministic Mongo `_id` for this event.

        AC4 contract: `f"{category}::{dedupe_key}::{timestamp_iso}"` so
        re-deliveries of the same event-window collapse into the same row.
        """
        return f"{self.category}::{self.dedupe_key}::{self.timestamp.isoformat()}"

    @staticmethod
    def from_nats_message(subject: str, body: dict[str, Any]) -> AlertEvent | None:
        """Parse a `alerts.>` NATS body into a typed event. Returns None if invalid.

        Subject convention: `alerts.<category-segments>.<dedupe-token>` —
        the dedupe token is the trailing segment (e.g. `<position_id>` for
        `alerts.position.reconciliation.mismatch.<position_id>`). Producers
        MAY override by providing an explicit `dedupe_key` in the body.
        """
        if not subject or not subject.startswith("alerts."):
            return None

        segments = subject.split(".")
        # Strip the `alerts.` prefix; the remainder is the category path
        # with the trailing dedupe token. When the body carries an explicit
        # `dedupe_key`, use that and treat the WHOLE remainder as category.
        category_segments = segments[1:]
        if not category_segments:
            return None

        explicit_dedupe = body.get("dedupe_key")
        if explicit_dedupe:
            category = ".".join(category_segments)
            dedupe_key = str(explicit_dedupe)
        elif len(category_segments) == 1:
            # `alerts.<single-token>` — there is no per-event id; fall back
            # to `decision_id` if present, else the message hash.
            category = category_segments[0]
            dedupe_key = str(
                body.get("decision_id")
                or body.get("dedupe_key")
                or _hash_for_dedupe(body)
            )
        else:
            category = ".".join(category_segments[:-1])
            dedupe_key = category_segments[-1]

        severity_raw = body.get("severity", "warning")
        try:
            severity = AlertSeverity(severity_raw)
        except ValueError:
            severity = AlertSeverity.WARNING

        raw_ts = body.get("timestamp")
        ts: datetime
        if isinstance(raw_ts, datetime):
            ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=UTC)
        elif isinstance(raw_ts, int | float):
            ts = datetime.fromtimestamp(float(raw_ts), tz=UTC)
        elif isinstance(raw_ts, str):
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        canonical_keys = {
            "category",
            "severity",
            "subsystem",
            "strategy_id",
            "message",
            "decision_id",
            "timestamp",
            "dedupe_key",
            "summarized_ids",
        }
        payload = {k: v for k, v in body.items() if k not in canonical_keys}

        return AlertEvent(
            category=category,
            severity=severity,
            subsystem=body.get("subsystem"),
            strategy_id=body.get("strategy_id"),
            message=str(body.get("message", "")),
            decision_id=body.get("decision_id"),
            payload=payload,
            timestamp=ts,
            dedupe_key=dedupe_key,
            summarized_ids=list(body.get("summarized_ids") or []),
        )


def _hash_for_dedupe(body: dict[str, Any]) -> str:
    """Deterministic fallback dedup key when no natural id exists."""
    import hashlib
    import json

    canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
