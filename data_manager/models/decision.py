"""CIO decision event model persisted to the `cio_decisions` audit-trail collection (P0.2b)."""

from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field


class DecisionEvent(BaseModel):
    """A `signals.trading.*` NATS message persisted into the `cio_decisions` collection.

    Publisher is `petrosa-cio` (post-routing legacy signal). Aligns with the
    cross-service identifier contract: `decision_id` is the unique key here
    (CIO has already assigned one before publishing onto `signals.trading.>`).
    The full CIO reasoning context (`metadata.cio_justification`,
    `metadata.thought_trace`, `metadata.correlation_id`) is captured under
    `reasoning` to satisfy FR27.
    """

    decision_id: str = Field(..., description="CIO decision identifier (unique)")
    strategy_id: str = Field(..., description="Strategy that produced the decision")
    timestamp: datetime = Field(..., description="Event timestamp from publisher")
    symbol: str | None = Field(default=None, description="Trading pair symbol")
    action: str | None = Field(default=None, description="Decision action verb")
    price: float | None = Field(default=None, description="Reference price")
    quantity: float | None = Field(default=None, description="Order quantity")
    confidence: float | None = Field(default=None, description="Publisher confidence")
    source: str | None = Field(default=None, description="Publisher identifier")
    reasoning: dict[str, Any] = Field(
        default_factory=dict,
        description="CIO reasoning context (justification, thought_trace, correlation_id)",
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
    ) -> "DecisionEvent | None":
        """Parse a NATS JSON body into a DecisionEvent. Returns None if invalid.

        Required fields: `decision_id`, `strategy_id`, `timestamp`. All others
        are best-effort and may be missing in older payload shapes.
        """
        decision_id = msg_data.get("decision_id")
        strategy_id = msg_data.get("strategy_id") or msg_data.get("strategy")
        raw_ts = msg_data.get("timestamp")

        if not decision_id or not strategy_id or not raw_ts:
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

        metadata = msg_data.get("metadata") or {}
        reasoning: dict[str, Any] = {}
        if isinstance(metadata, dict):
            for k in ("cio_justification", "thought_trace", "correlation_id"):
                if metadata.get(k) is not None:
                    reasoning[k] = metadata[k]

        canonical = {
            "decision_id",
            "strategy_id",
            "strategy",
            "timestamp",
            "symbol",
            "action",
            "price",
            "quantity",
            "confidence",
            "source",
            "metadata",
            "_trace_context",
            "_decision_context",
        }
        payload = {k: v for k, v in msg_data.items() if k not in canonical}

        return DecisionEvent(
            decision_id=str(decision_id),
            strategy_id=str(strategy_id),
            timestamp=ts,
            symbol=(str(msg_data["symbol"]) if msg_data.get("symbol") else None),
            action=(str(msg_data["action"]) if msg_data.get("action") else None),
            price=_maybe_float(msg_data.get("price")),
            quantity=_maybe_float(msg_data.get("quantity")),
            confidence=_maybe_float(msg_data.get("confidence")),
            source=(str(msg_data["source"]) if msg_data.get("source") else None),
            reasoning=reasoning,
            subject=subject,
            payload=payload,
        )
