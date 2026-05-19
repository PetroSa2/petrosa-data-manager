"""Execution event model persisted to the `execution_events` audit-trail collection (P0.2c)."""

from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field

# `event_type` must match the publisher contract — kept as a constant set so
# we can validate without coupling to an Enum (publishers may add new ones).
KNOWN_EVENT_TYPES = {"placed", "filled", "rejected", "partial_fill"}


class ExecutionEvent(BaseModel):
    """An `execution.events.*` NATS message persisted into `execution_events`.

    Publisher is `petrosa-tradeengine`. Aligns with the cross-service
    identifier contract (petrosa_k8s/docs/cross-service-identifier-contract.md):
    `decision_id` is required so that order outcomes can be joined back to
    the CIO decision that produced them. `order_id` and `event_type` are
    also required for the audit-trail to be meaningful.
    """

    decision_id: str = Field(..., description="CIO decision identifier")
    strategy_id: str = Field(..., description="Strategy that produced the decision")
    order_id: str = Field(..., description="Exchange / engine order identifier")
    event_type: str = Field(
        ..., description="placed | filled | rejected | partial_fill"
    )
    timestamp: datetime = Field(..., description="Event timestamp from publisher")
    reason: str | None = Field(
        default=None, description="Structured reason (esp. for rejected / partial_fill)"
    )
    symbol: str | None = Field(default=None, description="Trading pair symbol")
    side: str | None = Field(default=None, description="buy / sell")
    qty: float | None = Field(default=None, description="Requested order quantity")
    fill_qty: float | None = Field(
        default=None, description="Filled quantity (present on filled / partial_fill)"
    )
    price: float | None = Field(default=None, description="Reference / fill price")
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
    ) -> "ExecutionEvent | None":
        """Parse a NATS JSON body into an ExecutionEvent. Returns None if invalid.

        Required: `decision_id`, `strategy_id`, `order_id`, `event_type`,
        `timestamp`. All others are best-effort.
        """
        decision_id = msg_data.get("decision_id")
        strategy_id = msg_data.get("strategy_id") or msg_data.get("strategy")
        order_id = msg_data.get("order_id")
        event_type = msg_data.get("event_type")
        raw_ts = msg_data.get("timestamp")

        if not decision_id or not strategy_id or not order_id or not event_type:
            return None
        if not raw_ts:
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
            "order_id",
            "event_type",
            "timestamp",
            "reason",
            "symbol",
            "side",
            "qty",
            "fill_qty",
            "price",
            "_trace_context",
            "_decision_context",
            "_otel_trace_headers",
        }
        payload = {k: v for k, v in msg_data.items() if k not in canonical}

        return ExecutionEvent(
            decision_id=str(decision_id),
            strategy_id=str(strategy_id),
            order_id=str(order_id),
            event_type=str(event_type),
            timestamp=ts,
            reason=(str(msg_data["reason"]) if msg_data.get("reason") else None),
            symbol=(str(msg_data["symbol"]) if msg_data.get("symbol") else None),
            side=(str(msg_data["side"]) if msg_data.get("side") else None),
            qty=_maybe_float(msg_data.get("qty")),
            fill_qty=_maybe_float(msg_data.get("fill_qty")),
            price=_maybe_float(msg_data.get("price")),
            subject=subject,
            payload=payload,
        )
