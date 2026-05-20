"""Pnl-events NATS consumer that persists into `pnl_events` (P0.2d).

Mirrors `ExecutionEventsConsumer` / `DecisionConsumer` / `IntentConsumer`
exactly so that operational behaviour (queue sizing, worker concurrency,
OTel context propagation, DuplicateKeyError tolerance) stays uniform
across the four audit-trail subscribers (P0.2a/b/c/d).

The publisher side is owned by P4.1 (P&L computation) — this subscriber
lands ahead per the operator decision on the P0.2 umbrella (#140) so the
subscription, collection, and indexes exist as soon as the publisher
ships. Until then, the consumer no-ops (no messages arrive) without
disrupting the other three subscribers.
"""

import asyncio
import json
import logging
from typing import Any

from opentelemetry import trace
from prometheus_client import Counter, Histogram

try:
    from petrosa_otel import (
        extract_decision_context_from_nats,
        set_decision_context,
    )
except ImportError:

    def extract_decision_context_from_nats(message_dict: Any) -> dict[str, Any]:
        return {}

    def set_decision_context(span: Any, **attributes: Any) -> None:
        return None


import constants
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.pnl_event import PnlEvent
from data_manager.utils.nats_trace_propagator import NATSTracePropagator

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

PNL_EVENTS_COLLECTION = "pnl_events"

pnl_messages_received = Counter(
    "data_manager_pnl_messages_received_total",
    "Total pnl-event messages received from NATS",
    ["subject"],
)
pnl_messages_persisted = Counter(
    "data_manager_pnl_messages_persisted_total",
    "Total pnl-event messages successfully persisted",
)
pnl_messages_failed = Counter(
    "data_manager_pnl_messages_failed_total",
    "Total pnl-event messages that failed processing",
    ["error_type"],
)
pnl_processing_time = Histogram(
    "data_manager_pnl_processing_seconds",
    "Pnl-event message processing time in seconds",
)


class PnlConsumer:
    """Subscribes to `pnl.events.>` and persists each event to MongoDB `pnl_events`.

    The persistence path uses an `_id` composed of `decision_id` + `pnl_kind` +
    timestamp microseconds so that repeated mark-to-market snapshots for the
    same decision do not collide while still being deduplicated when the
    publisher resends the exact same event (DuplicateKeyError is tolerated
    silently).
    """

    def __init__(
        self,
        nats_client: NATSClient | None = None,
        db_manager: Any | None = None,
        subject: str | None = None,
    ) -> None:
        self.nats_client = nats_client or NATSClient()
        self.db_manager = db_manager
        self.subject = subject or constants.NATS_PNL_EVENTS_SUBJECT
        self.running = False
        self.subscription: Any = None
        self._message_queue: asyncio.Queue = asyncio.Queue(
            maxsize=constants.MESSAGE_QUEUE_SIZE
        )
        self._processing_tasks: list[asyncio.Task] = []
        self._owns_nats_client = nats_client is None

    async def start(self) -> bool:
        try:
            logger.info("Starting pnl consumer", extra={"subject": self.subject})

            if self._owns_nats_client and not await self.nats_client.connect():
                logger.error("Failed to connect to NATS for pnl consumer")
                return False

            await self._ensure_indexes()

            self.subscription = await self.nats_client.subscribe(
                subject=self.subject,
                callback=self._on_message,
            )
            if not self.subscription:
                logger.error(
                    "Failed to subscribe to pnl subject",
                    extra={"subject": self.subject},
                )
                return False

            self.running = True
            workers = min(constants.MAX_CONCURRENT_TASKS, 5)
            for i in range(workers):
                self._processing_tasks.append(asyncio.create_task(self._worker(i)))

            logger.info(
                "Pnl consumer started",
                extra={"subject": self.subject, "workers": workers},
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start pnl consumer: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        logger.info("Stopping pnl consumer")
        self.running = False

        if self._processing_tasks:
            for task in self._processing_tasks:
                task.cancel()
            await asyncio.gather(*self._processing_tasks, return_exceptions=True)

        if self.subscription:
            try:
                await self.subscription.unsubscribe()
            except Exception as e:
                logger.warning(f"Error unsubscribing pnl consumer: {e}")

        if self._owns_nats_client:
            await self.nats_client.disconnect()

        logger.info("Pnl consumer stopped")

    async def _ensure_indexes(self) -> None:
        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.warning("Pnl consumer: MongoDB unavailable; persistence will no-op")
            return
        try:
            await self.db_manager.mongodb_adapter.ensure_indexes(PNL_EVENTS_COLLECTION)
        except Exception as e:
            logger.warning(f"Failed to ensure indexes for {PNL_EVENTS_COLLECTION}: {e}")

    async def _on_message(self, msg: Any) -> None:
        try:
            await self._message_queue.put(msg)
        except asyncio.QueueFull:
            logger.warning("Pnl queue full; dropping message")
            pnl_messages_failed.labels(error_type="queue_full").inc()

    async def _worker(self, worker_id: int) -> None:
        logger.info(f"Pnl worker {worker_id} started")
        try:
            while self.running:
                try:
                    msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                try:
                    await self._process_message(msg)
                except Exception as e:
                    logger.error(f"Error in pnl worker {worker_id}: {e}", exc_info=True)
                    pnl_messages_failed.labels(error_type="processing").inc()
        except asyncio.CancelledError:
            pass
        logger.info(f"Pnl worker {worker_id} stopped")

    async def _process_message(self, msg: Any) -> None:
        start_time = asyncio.get_event_loop().time()
        subject = getattr(msg, "subject", self.subject)

        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Failed to decode pnl message: {e}")
            pnl_messages_failed.labels(error_type="json_decode").inc()
            return

        pnl_messages_received.labels(subject=subject).inc()

        with NATSTracePropagator.create_span_from_message(
            tracer,
            data,
            "persist_pnl_event",
            span_kind=trace.SpanKind.CONSUMER,
        ) as span:
            span.set_attribute("messaging.destination", subject)

            event = PnlEvent.from_nats_message(data, subject=subject)
            if event is None:
                span.set_attribute("message.invalid", True)
                logger.warning(
                    "invalid_pnl_event_received",
                    extra={"subject": subject, "raw_data": data},
                )
                pnl_messages_failed.labels(error_type="invalid_payload").inc()
                return

            # Span attribute names match the cross-service identifier contract.
            span.set_attribute("decision.decision_id", event.decision_id)
            span.set_attribute("pnl.kind", event.pnl_kind)
            if event.order_id:
                span.set_attribute("execution.order_id", event.order_id)

            decision_attrs: dict[str, Any] = {
                "decision_id": event.decision_id,
                "strategy_id": event.strategy_id,
            }
            embedded_ctx = extract_decision_context_from_nats(data)
            for k, v in embedded_ctx.items():
                decision_attrs.setdefault(k, v)
            set_decision_context(span, **decision_attrs)

            log_extra = {
                "subject": subject,
                "decision_id": event.decision_id,
                "strategy_id": event.strategy_id,
                "pnl_kind": event.pnl_kind,
            }

            persisted = await self._persist(event)
            if persisted:
                pnl_messages_persisted.inc()
                logger.info("pnl_event_persisted", extra=log_extra)
                span.set_status(trace.Status(trace.StatusCode.OK))
            else:
                pnl_messages_failed.labels(error_type="persistence").inc()
                logger.warning("pnl_event_persistence_skipped", extra=log_extra)
                span.set_attribute("persistence.skipped", True)

            pnl_processing_time.observe(asyncio.get_event_loop().time() - start_time)

    async def _persist(self, event: PnlEvent) -> bool:
        adapter = (
            getattr(self.db_manager, "mongodb_adapter", None)
            if self.db_manager
            else None
        )
        if adapter is None or getattr(adapter, "db", None) is None:
            return False
        try:
            doc = event.model_dump(exclude_none=True)
            # `_id` composes decision_id + pnl_kind + timestamp microseconds so
            # repeated mark-to-market snapshots for the same decision can coexist
            # while genuine duplicates (same kind + same instant) deduplicate.
            ts_micro = int(event.timestamp.timestamp() * 1_000_000)
            doc["_id"] = f"{event.decision_id}:{event.pnl_kind}:{ts_micro}"
            doc = adapter._prepare_for_bson(doc)
            try:
                from pymongo.errors import DuplicateKeyError
            except ImportError:  # pragma: no cover - pymongo ships with motor
                DuplicateKeyError = Exception  # type: ignore[assignment, misc]
            try:
                await adapter.db[PNL_EVENTS_COLLECTION].insert_one(doc)
                return True
            except DuplicateKeyError:
                logger.debug(
                    "pnl_event_already_persisted",
                    extra={
                        "decision_id": event.decision_id,
                        "pnl_kind": event.pnl_kind,
                    },
                )
                return True
        except Exception as e:
            logger.error(
                f"Failed to persist pnl event {event.decision_id}:{event.pnl_kind}: {e}"
            )
            return False
