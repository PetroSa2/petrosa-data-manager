"""Publishes `pnl.events.<strategy_id>` for every persisted fill (P4.1 follow-up, petrosa_k8s#652).

This module wires the `ExecutionEventsConsumer.on_persisted` hook surface
shipped in #601 (PR data-manager#151) to a live NATS publisher so the
P0.1-contract subject `pnl.events.<strategy_id>` actually carries traffic
in production. Downstream consumers (drawdown evaluator P4.2, dashboard
P5.1, audit-trail subscriber P0.2d) become production-meaningful once
this binding is live.

Design
------

`PnlEventPublisher` owns:

  * a long-lived `PnlCalculator` (FIFO lot state must survive between
    fills — recreating per-event would mis-state realized P&L), and
  * a `NatsClientLike` publisher (any object exposing
    ``async publish(subject, payload)`` — typically the
    `_DeferredNatsClient` already used by the ingest / execution /
    audit evaluators in `data_manager/main.py`).

Each fill produces at most one `PnlEvent`:

  * ``pnl_kind="closed"`` when the fill matched against opposing lots
    (``realized_pnl != 0``). ``realized_pnl_usd`` is the delta for this
    fill, NOT the running tally — downstream summing is the consumer's
    job.
  * ``pnl_kind="mark_to_market"`` when the fill opened (or re-opened)
    lots without closing any (``realized_pnl == 0`` and
    ``opened_qty > 0``). ``unrealized_pnl_usd`` carries the current
    strategy-scope unrealized using the just-traded fill price as the
    mark (consistent with `PnlCalculator.apply_fill`).
  * no event when the row isn't a usable fill (defensive — the consumer
    only fires the hook on `placed/filled/rejected/partial_fill` events,
    but `apply_fill` filters strictly to fill rows with positive
    qty/price).

Errors from the underlying NATS publish are logged at WARNING and
swallowed; the consumer's existing hook wrapper also swallows them,
so a broken broker cannot poison the consume path.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Protocol

try:
    from datetime import UTC
except ImportError:  # Python 3.10 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from data_manager.models.execution_event import ExecutionEvent
from data_manager.models.pnl_event import PnlEvent
from data_manager.services.pnl_calculator import PnlCalculator

logger = logging.getLogger(__name__)


class NatsClientLike(Protocol):
    """The subset of nats-py's client surface this publisher depends on."""

    async def publish(self, subject: str, payload: bytes) -> None: ...


# Mirrors `data_manager/models/execution_event.py:KNOWN_EVENT_TYPES`.
_FILL_EVENT_TYPES = frozenset({"filled", "partial_fill"})


class PnlEventPublisher:
    """Binds the `ExecutionEventsConsumer.on_persisted` hook to a NATS publisher.

    Instantiate once per process and pass `publisher.on_persisted` as the
    `on_persisted=` argument when constructing `ExecutionEventsConsumer`.
    """

    def __init__(
        self,
        *,
        nats_client: NatsClientLike,
        calculator: PnlCalculator | None = None,
        subject_prefix: str = "pnl.events",
    ) -> None:
        self._nats_client = nats_client
        self._calculator = calculator if calculator is not None else PnlCalculator()
        self._subject_prefix = subject_prefix.rstrip(".")

    @property
    def calculator(self) -> PnlCalculator:
        """Expose the calculator so callers (e.g. seed-replay or tests) can prime it."""
        return self._calculator

    def replay_history(self, events: list[dict[str, Any]]) -> int:
        """Replay historical execution events into the calculator without publishing.

        Used at cold-start to seed FIFO lot state from `execution_events` so
        the first live fill produces realistic realized-P&L numbers. Returns
        the count of rows that actually moved lot state (non-`None`
        `apply_fill` impacts).
        """
        applied = 0
        for row in events:
            if self._calculator.apply_fill(row) is not None:
                applied += 1
        return applied

    async def on_persisted(self, event: ExecutionEvent) -> None:
        """The hook bound to `ExecutionEventsConsumer(on_persisted=...)`.

        Computes the delta-P&L for the just-persisted fill and publishes
        one `pnl.events.<strategy_id>` message when warranted. Silently
        ignores non-fill events. Logs (but does not raise) NATS publish
        errors so the consumer's main path stays clean.
        """
        impact = self._calculator.apply_fill(_event_to_fill(event))
        if impact is None:
            return

        pnl_event = _build_pnl_event(event, impact, self._calculator)
        if pnl_event is None:
            return

        subject = f"{self._subject_prefix}.{event.strategy_id}"
        body = pnl_event.model_dump(mode="json", exclude_none=True)
        try:
            payload = json.dumps(body, default=_json_default).encode()
            await self._nats_client.publish(subject, payload)
        except Exception as exc:
            logger.warning(
                "pnl_event_publish_failed",
                extra={
                    "subject": subject,
                    "decision_id": event.decision_id,
                    "strategy_id": event.strategy_id,
                    "pnl_kind": pnl_event.pnl_kind,
                    "error": str(exc),
                },
            )
            return

        logger.info(
            "pnl_event_published",
            extra={
                "subject": subject,
                "decision_id": event.decision_id,
                "strategy_id": event.strategy_id,
                "pnl_kind": pnl_event.pnl_kind,
                "realized_pnl_usd": pnl_event.realized_pnl_usd,
                "unrealized_pnl_usd": pnl_event.unrealized_pnl_usd,
            },
        )


def _event_to_fill(event: ExecutionEvent) -> dict[str, Any]:
    """Translate an `ExecutionEvent` into the dict shape `PnlCalculator.apply_fill` expects."""
    return {
        "event_type": event.event_type,
        "strategy_id": event.strategy_id,
        "symbol": event.symbol,
        "side": event.side,
        "fill_qty": event.fill_qty,
        "qty": event.qty,
        "price": event.price,
    }


def _build_pnl_event(
    event: ExecutionEvent,
    impact: Any,  # FillImpact, but typed loosely to keep this module decoupled.
    calculator: PnlCalculator,
) -> PnlEvent | None:
    """Choose `pnl_kind` and assemble the `PnlEvent` body."""
    if event.event_type not in _FILL_EVENT_TYPES:
        return None  # defensive — apply_fill already returned None in that case

    timestamp = (
        event.timestamp
        if event.timestamp.tzinfo
        else event.timestamp.replace(tzinfo=UTC)
    )
    realized = float(impact.realized_pnl)

    if realized != 0.0:
        return PnlEvent(
            decision_id=event.decision_id,
            strategy_id=event.strategy_id,
            order_id=event.order_id,
            timestamp=timestamp,
            pnl_kind="closed",
            realized_pnl_usd=realized,
            currency="USD",
        )

    if impact.opened_qty > 0:
        breakdown = calculator.strategy_pnl(event.strategy_id)
        return PnlEvent(
            decision_id=event.decision_id,
            strategy_id=event.strategy_id,
            order_id=event.order_id,
            timestamp=timestamp,
            pnl_kind="mark_to_market",
            unrealized_pnl_usd=float(breakdown.unrealized),
            currency="USD",
        )

    return None


def _json_default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


__all__ = ["NatsClientLike", "PnlEventPublisher"]
