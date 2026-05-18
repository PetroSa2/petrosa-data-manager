"""CIO intent event model persisted to the `intents` audit-trail collection (P0.2a)."""

from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field


class IntentEvent(BaseModel):
    """A `cio.intent.*` NATS message persisted into the `intents` collection.

    Aligns with the cross-service identifier contract
    (petrosa_k8s/docs/cross-service-identifier-contract.md). `decision_id`
    is nullable because publishers (strategy services) may emit before the
    CIO has assigned one — the pairing happens post-CIO.
    """

    intent_id: str = Field(..., description="Unique per-intent identifier")
    strategy_id: str = Field(..., description="Strategy that produced the intent")
    timestamp: datetime = Field(..., description="Event timestamp from publisher")
    decision_id: str | None = Field(
        default=None, description="CIO decision_id (assigned post-CIO; nullable)"
    )
    symbol: str | None = Field(default=None, description="Trading pair symbol")
    action: str | None = Field(default=None, description="Intent action verb")
    confidence: float | None = Field(default=None, description="Publisher confidence")
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
    ) -> "IntentEvent | None":
        """Parse a NATS JSON body into an IntentEvent. Returns None if invalid.

        Required fields: `intent_id`, `strategy_id`, `timestamp`. All others
        are best-effort and may be missing in older payload shapes.
        """
        intent_id = msg_data.get("intent_id")
        strategy_id = msg_data.get("strategy_id")
        raw_ts = msg_data.get("timestamp")

        if not intent_id or not strategy_id or not raw_ts:
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

        confidence = msg_data.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = None

        payload = {
            k: v
            for k, v in msg_data.items()
            if k
            not in {
                "intent_id",
                "strategy_id",
                "timestamp",
                "decision_id",
                "symbol",
                "action",
                "confidence",
                "_trace_context",
                "_decision_context",
            }
        }

        return IntentEvent(
            intent_id=str(intent_id),
            strategy_id=str(strategy_id),
            timestamp=ts,
            decision_id=(
                str(msg_data["decision_id"]) if msg_data.get("decision_id") else None
            ),
            symbol=(str(msg_data["symbol"]) if msg_data.get("symbol") else None),
            action=(str(msg_data["action"]) if msg_data.get("action") else None),
            confidence=confidence,
            subject=subject,
            payload=payload,
        )
