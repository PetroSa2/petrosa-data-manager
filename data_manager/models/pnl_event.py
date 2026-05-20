"""Pnl event model persisted to the `pnl_events` audit-trail collection (P0.2d).

Publisher will be `petrosa-data-manager`'s P&L computation path itself (P4.1).
This subscriber side lands ahead of the publisher per the operator decision
recorded on the P0.2 umbrella (#140) so the audit-trail subscription, the
collection, and the indexes exist as soon as the publisher ships.

Aligns with the cross-service identifier contract:
`docs/cross-service-identifier-contract.md`. `decision_id` is required so
that P&L outcomes can be joined back to the CIO decision and the
execution event(s) that produced them.
"""

from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field

# `pnl_kind` constrains the publisher to a known vocabulary. Subscribers tolerate
# unknown values gracefully (they persist with whatever kind was sent), but the
# documented kinds give downstream queries (FR22, FR9 lifecycle reconstruction)
# a stable contract to filter on.
KNOWN_PNL_KINDS = {"closed", "mark_to_market", "aggregate"}


class PnlEvent(BaseModel):
    """A `pnl.events.<strategy_id>` NATS message persisted into `pnl_events`.

    Required: `decision_id`, `strategy_id`, `timestamp`, `pnl_kind`.

    Optional (publisher-dependent):
        * `order_id` — present for `closed` kind tied to a specific order
        * `position_id` — present for `mark_to_market` and `aggregate` kinds
        * `realized_pnl_usd` — present when the kind is `closed`
        * `unrealized_pnl_usd` — present when the kind is `mark_to_market`
        * `currency` — defaults to USD; carried verbatim for non-USD strategies

    The `payload` field captures any extra fields the publisher emitted, so
    schema evolution does not require a model migration on the subscriber.
    """

    decision_id: str = Field(..., description="CIO decision identifier")
    strategy_id: str = Field(..., description="Strategy that produced the decision")
    timestamp: datetime = Field(..., description="Event timestamp from publisher")
    pnl_kind: str = Field(
        ..., description="closed | mark_to_market | aggregate (publisher-defined)"
    )
    realized_pnl_usd: float | None = Field(
        default=None, description="Realized P&L in USD (closed kind)"
    )
    unrealized_pnl_usd: float | None = Field(
        default=None, description="Mark-to-market P&L in USD"
    )
    currency: str | None = Field(
        default=None, description="Currency code; defaults to USD on the consumer side"
    )
    order_id: str | None = Field(
        default=None, description="Exchange / engine order identifier (closed kind)"
    )
    position_id: str | None = Field(
        default=None, description="Position aggregate identifier"
    )
    subject: str | None = Field(default=None, description="Originating NATS subject")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Full message body (post-trace-strip)"
    )
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Subscriber persistence timestamp",
    )

    @staticmethod
    def from_nats_message(
        msg_data: dict[str, Any], subject: str | None = None
    ) -> "PnlEvent | None":
        """Parse a NATS JSON body into a PnlEvent. Returns None if invalid.

        Required: `decision_id`, `strategy_id`, `pnl_kind`, `timestamp`. All
        others are best-effort; the subscriber tolerates absent optional
        fields rather than rejecting the message.
        """
        decision_id = msg_data.get("decision_id")
        strategy_id = msg_data.get("strategy_id") or msg_data.get("strategy")
        pnl_kind = msg_data.get("pnl_kind") or msg_data.get("kind")
        raw_ts = msg_data.get("timestamp")

        if not decision_id or not strategy_id or not pnl_kind or not raw_ts:
            return None

        try:
            if isinstance(raw_ts, datetime):
                ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=UTC)
            elif isinstance(raw_ts, int | float):
                ts = datetime.fromtimestamp(float(raw_ts), tz=UTC)
            else:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

        def _maybe_float(v: Any) -> float | None:
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        canonical = {
            "decision_id",
            "strategy_id",
            "strategy",
            "pnl_kind",
            "kind",
            "timestamp",
            "realized_pnl_usd",
            "unrealized_pnl_usd",
            "currency",
            "order_id",
            "position_id",
            "_trace_context",
            "_decision_context",
            "_otel_trace_headers",
        }
        payload = {k: v for k, v in msg_data.items() if k not in canonical}

        return PnlEvent(
            decision_id=str(decision_id),
            strategy_id=str(strategy_id),
            timestamp=ts,
            pnl_kind=str(pnl_kind),
            realized_pnl_usd=_maybe_float(msg_data.get("realized_pnl_usd")),
            unrealized_pnl_usd=_maybe_float(msg_data.get("unrealized_pnl_usd")),
            currency=(str(msg_data["currency"]) if msg_data.get("currency") else None),
            order_id=(str(msg_data["order_id"]) if msg_data.get("order_id") else None),
            position_id=(
                str(msg_data["position_id"]) if msg_data.get("position_id") else None
            ),
            subject=subject,
            payload=payload,
        )
