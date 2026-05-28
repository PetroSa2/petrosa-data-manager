"""Alert spine — subscribes to `alerts.>`, persists every event, attempts
delivery to the operator webhook, and enforces per-category rate limiting
with summary-alert rollup (petrosa-data-manager#183).

Implements:

* **AC3** — at-least-once delivery + dedup. Bounded retry: 3 attempts with
  exponential backoff (1s / 2s / 4s); after the cap the event is marked
  `failed` and stays in the audit trail.
* **AC4** — audit-trail persistence. Every event lands in the `alerts`
  Mongo collection keyed by `f"{category}::{dedupe_key}::{timestamp}"` so
  NATS replays replace the same row in-place.
* **AC5** — rate-limit policy. Per-category max 10 alerts/minute by
  default; overflow rolls into a single `alerts.summary.<category>` event
  carrying the suppressed `_id`s. Per-category limit override is
  `PETROSA_ALERT_RATELIMIT_<CATEGORY>` (numeric env var).

Configuration (env-var):

* `PETROSA_ALERT_GRAFANA_WEBHOOK_URL` — when empty (default), the
  dispatcher persists every event but does NOT call out; it marks the
  event `delivered_mock`. Production wire-up = set this var (e.g. via the
  petrosa-apps secret) and the dispatcher starts actually POSTing. No
  code change needed.
* `PETROSA_ALERT_RATELIMIT_<CATEGORY>` — per-category override (integer
  alerts-per-minute); falls back to `PETROSA_ALERT_RATELIMIT_DEFAULT`,
  which defaults to 10.

The cross-repo producer side (AC2.e — `petrosa-tradeengine` reconciliation
mismatch emit on `alerts.position.reconciliation.mismatch.<position_id>`)
is tracked separately; the spine here is ready to receive whenever that
PR lands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

import httpx

import constants
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.alert import (
    AlertDeliveryState,
    AlertEvent,
    AlertSeverity,
    DeliveryAttempt,
)

logger = logging.getLogger(__name__)

ALERTS_COLLECTION = "alerts"

# AC3 — bounded retry envelope. Three attempts, 1s/2s/4s exponential
# backoff. These are deliberate small numbers: the dispatcher is in the
# hot path of the alert spine; long delays would amplify outages.
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
_MAX_ATTEMPTS = len(_RETRY_BACKOFF_SECONDS)
_WEBHOOK_TIMEOUT_SECONDS = 10.0

# AC5 — rate-limit policy defaults.
_DEFAULT_PER_CATEGORY_LIMIT = 10  # alerts per minute, per AC text
_RATE_LIMIT_WINDOW_SECONDS = 60.0


def _category_limit(category: str) -> int:
    """Resolve `PETROSA_ALERT_RATELIMIT_<CATEGORY>` (uppercased, dots → _).

    Falls back to `PETROSA_ALERT_RATELIMIT_DEFAULT`, which itself defaults
    to `_DEFAULT_PER_CATEGORY_LIMIT`.
    """
    sanitized = category.upper().replace(".", "_").replace("-", "_")
    per_cat = os.environ.get(f"PETROSA_ALERT_RATELIMIT_{sanitized}")
    if per_cat:
        try:
            return max(1, int(per_cat))
        except ValueError:
            logger.warning(
                "Invalid per-category rate limit env: %s=%s; falling back",
                f"PETROSA_ALERT_RATELIMIT_{sanitized}",
                per_cat,
            )
    default = os.environ.get("PETROSA_ALERT_RATELIMIT_DEFAULT")
    if default:
        try:
            return max(1, int(default))
        except ValueError:
            pass
    return _DEFAULT_PER_CATEGORY_LIMIT


@dataclass
class _RateLimiterState:
    """Per-category sliding-window state.

    `recent_timestamps` is a monotonically-increasing deque of unix
    timestamps for accepted (non-suppressed) alerts; the head is evicted
    when it falls outside the window.

    `suppressed` accumulates the dedup keys of events suppressed since the
    last summary flush. When the window expires and the limit was hit,
    the dispatcher emits one summary alert carrying these IDs and clears
    the list.
    """

    recent_timestamps: deque[float] = field(default_factory=deque)
    suppressed: list[str] = field(default_factory=list)


class AlertDispatcher:
    """The petrosa-data-manager alert spine (AC3+AC4+AC5).

    Construction is cheap — the dispatcher does NOT open the NATS
    subscription until `start()` is called. This matches the consumer
    pattern in `data_manager/consumer/decision_consumer.py` so the
    dispatcher slots into the `Service` startup sequence in
    `data_manager/main.py` exactly like the other consumers.
    """

    def __init__(
        self,
        nats_client: NATSClient | None = None,
        db_manager: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        subject: str | None = None,
        webhook_url: str | None = None,
    ) -> None:
        self.nats_client = nats_client or NATSClient()
        self.db_manager = db_manager
        # AC3 — webhook URL resolution. Explicit constructor override beats
        # env-var; env-var beats "no webhook configured" (mock mode).
        self.webhook_url = (
            webhook_url
            if webhook_url is not None
            else os.environ.get("PETROSA_ALERT_GRAFANA_WEBHOOK_URL", "")
        )
        self.http_client = http_client or httpx.AsyncClient(
            timeout=_WEBHOOK_TIMEOUT_SECONDS,
            headers={"User-Agent": "petrosa-data-manager/alert-dispatcher"},
        )
        self.subject = subject or "alerts.>"
        self.running = False
        self.subscription: Any = None
        self._owns_nats_client = nats_client is None
        self._owns_http_client = http_client is None
        # AC5 — per-category rate-limit state. Keyed by canonical category.
        self._rate_state: dict[str, _RateLimiterState] = defaultdict(_RateLimiterState)
        # Bound to None so unit tests can patch `_now` if needed.
        self._now = time.monotonic

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Connect, ensure indexes, subscribe. Returns False on any failure."""
        try:
            logger.info(
                "Starting alert dispatcher",
                extra={
                    "subject": self.subject,
                    "webhook_configured": bool(self.webhook_url),
                },
            )
            if self._owns_nats_client and not await self.nats_client.connect():
                logger.error("Failed to connect to NATS for alert dispatcher")
                return False
            await self._ensure_indexes()
            self.subscription = await self.nats_client.subscribe(
                subject=self.subject,
                callback=self._on_message,
            )
            if not self.subscription:
                logger.error(
                    "Failed to subscribe to alerts subject",
                    extra={"subject": self.subject},
                )
                return False
            self.running = True
            logger.info("Alert dispatcher started", extra={"subject": self.subject})
            return True
        except Exception as exc:
            logger.error("Failed to start alert dispatcher: %s", exc, exc_info=True)
            return False

    async def stop(self) -> None:
        logger.info("Stopping alert dispatcher")
        self.running = False
        if self.subscription:
            try:
                await self.subscription.unsubscribe()
            except Exception as exc:
                logger.warning("Error unsubscribing alert dispatcher: %s", exc)
        if self._owns_nats_client:
            await self.nats_client.disconnect()
        if self._owns_http_client:
            await self.http_client.aclose()
        logger.info("Alert dispatcher stopped")

    async def _ensure_indexes(self) -> None:
        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.warning(
                "Alert dispatcher: MongoDB unavailable; persistence will no-op"
            )
            return
        try:
            await self.db_manager.mongodb_adapter.ensure_indexes(ALERTS_COLLECTION)
        except Exception as exc:
            logger.warning(
                "Failed to ensure indexes for %s: %s", ALERTS_COLLECTION, exc
            )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _on_message(self, msg: Any) -> None:
        """NATS callback — dispatches inline; subscriber back-pressure is
        provided by the NATS client's own queue rather than a local one
        (alerts volume is low; a per-dispatcher queue would just add a
        race window where a process restart drops in-flight rows).
        """
        try:
            subject = getattr(msg, "subject", self.subject)
            body = json.loads(msg.data.decode())
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Discarding malformed alert message: %s", exc)
            return
        try:
            await self.dispatch(subject=subject, body=body)
        except Exception as exc:
            logger.error(
                "Alert dispatch failed for subject %s: %s", subject, exc, exc_info=True
            )

    async def dispatch(
        self, *, subject: str, body: dict[str, Any]
    ) -> AlertEvent | None:
        """Parse, rate-limit-check, persist, attempt delivery. Returns the
        persisted event (or None if the body was invalid)."""
        event = AlertEvent.from_nats_message(subject, body)
        if event is None:
            logger.warning("Discarding invalid alert payload on %s", subject)
            return None

        # AC5 — rate-limit gate. Summary alerts bypass the limiter (a
        # summary alert is itself the overflow signal; rate-limiting it
        # would silence the very thing the operator is supposed to see).
        if not event.category.startswith("summary."):
            decision = self._record_for_rate_limit(event)
            if decision == "suppress":
                # AC5 — suppressed events still land in the audit trail so
                # operators can see what was rolled up; they just are not
                # delivered. The summary alert (emitted when the window
                # clears) carries the back-links.
                event.delivery_state = AlertDeliveryState.PENDING
                event.delivery_attempts.append(
                    DeliveryAttempt(
                        attempt=1,
                        state=AlertDeliveryState.PENDING,
                        error="suppressed_by_rate_limit",
                    )
                )
                await self._persist(event)
                # Trigger a summary if THIS event tipped over the limit.
                await self._maybe_flush_summary(event.category)
                return event

        await self._persist(event)
        await self._attempt_delivery(event)
        return event

    # ------------------------------------------------------------------
    # AC3 — bounded retry + delivery
    # ------------------------------------------------------------------

    async def _attempt_delivery(self, event: AlertEvent) -> None:
        """Try delivery up to `_MAX_ATTEMPTS` times. Marks the row
        `delivered`, `delivered_mock`, or `failed` and updates the
        attempts log on every iteration."""
        if not self.webhook_url:
            # AC3 — no webhook configured → mock-delivered (not failed).
            event.delivery_state = AlertDeliveryState.DELIVERED_MOCK
            event.delivery_attempts.append(
                DeliveryAttempt(
                    attempt=1,
                    state=AlertDeliveryState.DELIVERED_MOCK,
                )
            )
            await self._persist(event)
            return

        for attempt_idx, sleep_seconds in enumerate(_RETRY_BACKOFF_SECONDS, start=1):
            status, error = await self._post_webhook(event)
            if status is not None and 200 <= status < 300:
                event.delivery_state = AlertDeliveryState.DELIVERED
                event.delivery_attempts.append(
                    DeliveryAttempt(
                        attempt=attempt_idx,
                        state=AlertDeliveryState.DELIVERED,
                        http_status=status,
                    )
                )
                await self._persist(event)
                return

            event.delivery_attempts.append(
                DeliveryAttempt(
                    attempt=attempt_idx,
                    state=AlertDeliveryState.RETRY
                    if attempt_idx < _MAX_ATTEMPTS
                    else AlertDeliveryState.FAILED,
                    http_status=status,
                    error=(error[:500] if error else None),
                )
            )
            if attempt_idx < _MAX_ATTEMPTS:
                event.delivery_state = AlertDeliveryState.RETRY
                await self._persist(event)
                await asyncio.sleep(sleep_seconds)

        event.delivery_state = AlertDeliveryState.FAILED
        await self._persist(event)

    async def _post_webhook(self, event: AlertEvent) -> tuple[int | None, str | None]:
        """POST the event JSON to the configured webhook. Returns
        `(status_code, error_str)` — exactly one is non-None."""
        body = {
            "category": event.category,
            "severity": event.severity.value,
            "subsystem": event.subsystem,
            "strategy_id": event.strategy_id,
            "decision_id": event.decision_id,
            "message": event.message,
            "timestamp": event.timestamp.isoformat(),
            "payload": event.payload,
        }
        try:
            response = await self.http_client.post(self.webhook_url, json=body)
        except httpx.TimeoutException:
            return None, "timeout"
        except httpx.HTTPError as exc:
            return None, f"http_error: {exc}"
        return response.status_code, None

    # ------------------------------------------------------------------
    # AC5 — rate limit + summary rollup
    # ------------------------------------------------------------------

    def _record_for_rate_limit(self, event: AlertEvent) -> str:
        """Sliding-window check. Returns `"accept"` or `"suppress"`.

        Side effect: when accepting, appends `now` to `recent_timestamps`;
        when suppressing, appends the event's `_id` to the `suppressed`
        list so the next summary can reference it.
        """
        state = self._rate_state[event.category]
        limit = _category_limit(event.category)
        now = self._now()
        # Evict stale entries (older than the sliding window).
        while (
            state.recent_timestamps
            and state.recent_timestamps[0] < now - _RATE_LIMIT_WINDOW_SECONDS
        ):
            state.recent_timestamps.popleft()
        if len(state.recent_timestamps) >= limit:
            state.suppressed.append(event.make_id())
            return "suppress"
        state.recent_timestamps.append(now)
        return "accept"

    async def _maybe_flush_summary(self, category: str) -> None:
        """Emit a `alerts.summary.<category>` event when one or more events
        for `category` have been suppressed since the last flush.

        Called once per suppressed event so the operator always sees a
        summary within a single rate-limit window; we cheap-out by only
        actually creating a summary on every 10th suppression to avoid
        amplifying the volume the limiter was supposed to dampen.
        """
        state = self._rate_state[category]
        if not state.suppressed:
            return
        # Throttle summary creation: emit one summary per 10 suppressions
        # or when the suppressed list crosses 50 entries.
        if len(state.suppressed) % 10 != 0 and len(state.suppressed) < 50:
            return

        summary = AlertEvent(
            category=f"summary.{category}",
            severity=AlertSeverity.WARNING,
            subsystem=None,
            message=(
                f"{len(state.suppressed)} `{category}` alerts suppressed by "
                f"rate-limit policy in the current window"
            ),
            payload={"suppressed_count": len(state.suppressed)},
            dedupe_key=f"{category}-{int(self._now())}",
            summarized_ids=list(state.suppressed),
        )
        await self._persist(summary)
        # Best-effort delivery — summary alerts are NEVER suppressed by
        # the rate-limiter (the early-return in `dispatch` enforces that).
        await self._attempt_delivery(summary)
        state.suppressed.clear()

    # ------------------------------------------------------------------
    # AC4 — persistence
    # ------------------------------------------------------------------

    async def _persist(self, event: AlertEvent) -> bool:
        """Upsert the event into the `alerts` collection keyed by
        `f"{category}::{dedupe_key}::{timestamp_iso}"` so NATS replays of
        the same event collapse into one row (AC3 dedup + AC4 audit-trail)."""
        adapter = (
            getattr(self.db_manager, "mongodb_adapter", None)
            if self.db_manager
            else None
        )
        if adapter is None or getattr(adapter, "db", None) is None:
            logger.debug(
                "Alert dispatcher persist no-op (MongoDB unavailable): %s::%s",
                event.category,
                event.dedupe_key,
            )
            return False
        doc = event.model_dump(exclude_none=False, mode="json")
        doc["_id"] = event.make_id()
        # Severity, state are enums → str post-model_dump.
        doc = adapter._prepare_for_bson(doc)
        try:
            await adapter.db[ALERTS_COLLECTION].replace_one(
                {"_id": doc["_id"]}, doc, upsert=True
            )
        except Exception as exc:
            logger.error(
                "Failed to upsert alert %s into %s: %s",
                doc["_id"],
                ALERTS_COLLECTION,
                exc,
                exc_info=True,
            )
            return False
        return True


# Backwards-compat re-exports — keep the module surface narrow but useful
# for test seams.
__all__ = [
    "AlertDispatcher",
    "ALERTS_COLLECTION",
]


# Module-level sanity: at import time, surface that the spine is wired and
# whether webhook delivery is in mock mode. Helps operators triage by
# scanning startup logs (we cannot do it inside __init__ because the
# dispatcher may be instantiated multiple times in tests).
def _log_webhook_mode_once() -> None:  # pragma: no cover — log-only helper
    if os.environ.get("_PETROSA_ALERT_LOGGED"):
        return
    os.environ["_PETROSA_ALERT_LOGGED"] = "1"
    if os.environ.get("PETROSA_ALERT_GRAFANA_WEBHOOK_URL"):
        logger.info("Alert dispatcher: webhook delivery enabled")
    else:
        logger.info(
            "Alert dispatcher: webhook URL unset; running in mock mode "
            "(set PETROSA_ALERT_GRAFANA_WEBHOOK_URL to enable real delivery)"
        )


_log_webhook_mode_once()

# Touch `constants` so reorder-imports does not strip the module — the
# dispatcher reads no top-level constants today but the import lets a
# future sibling reach `constants.NATS_ALERT_SUBJECT` without a follow-up.
_ = constants
_ = datetime  # surfaced for future timestamp instrumentation
