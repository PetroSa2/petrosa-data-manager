"""Execution-events NATS consumer that persists into `execution_events` (P0.2c)."""

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
from data_manager.models.execution_event import ExecutionEvent
from data_manager.utils.nats_trace_propagator import NATSTracePropagator

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

EXECUTION_EVENTS_COLLECTION = "execution_events"

execution_messages_received = Counter(
    "data_manager_execution_messages_received_total",
    "Total execution-event messages received from NATS",
    ["subject"],
)
execution_messages_persisted = Counter(
    "data_manager_execution_messages_persisted_total",
    "Total execution-event messages successfully persisted",
)
execution_messages_failed = Counter(
    "data_manager_execution_messages_failed_total",
    "Total execution-event messages that failed processing",
    ["error_type"],
)
execution_processing_time = Histogram(
    "data_manager_execution_processing_seconds",
    "Execution-event message processing time in seconds",
)


class ExecutionEventsConsumer:
    """Subscribes to `execution.events.>` and persists each event to MongoDB.

    Mirrors `DecisionConsumer` / `IntentConsumer` exactly so that operational
    behaviour (queue sizing, worker concurrency, OTel context propagation,
    DuplicateKeyError tolerance) stays uniform across the four audit-trail
    subscribers (P0.2a/b/c/d).
    """

    def __init__(
        self,
        nats_client: NATSClient | None = None,
        db_manager: Any | None = None,
        subject: str | None = None,
    ) -> None:
        self.nats_client = nats_client or NATSClient()
        self.db_manager = db_manager
        self.subject = subject or constants.NATS_EXECUTION_EVENTS_SUBJECT
        self.running = False
        self.subscription: Any = None
        self._message_queue: asyncio.Queue = asyncio.Queue(
            maxsize=constants.MESSAGE_QUEUE_SIZE
        )
        self._processing_tasks: list[asyncio.Task] = []
        self._owns_nats_client = nats_client is None

    async def start(self) -> bool:
        try:
            logger.info(
                "Starting execution events consumer", extra={"subject": self.subject}
            )

            if self._owns_nats_client and not await self.nats_client.connect():
                logger.error("Failed to connect to NATS for execution events consumer")
                return False

            await self._ensure_indexes()

            self.subscription = await self.nats_client.subscribe(
                subject=self.subject,
                callback=self._on_message,
            )
            if not self.subscription:
                logger.error(
                    "Failed to subscribe to execution events subject",
                    extra={"subject": self.subject},
                )
                return False

            self.running = True
            workers = min(constants.MAX_CONCURRENT_TASKS, 5)
            for i in range(workers):
                self._processing_tasks.append(asyncio.create_task(self._worker(i)))

            logger.info(
                "Execution events consumer started",
                extra={"subject": self.subject, "workers": workers},
            )
            return True
        except Exception as e:
            logger.error(
                f"Failed to start execution events consumer: {e}", exc_info=True
            )
            return False

    async def stop(self) -> None:
        logger.info("Stopping execution events consumer")
        self.running = False

        if self._processing_tasks:
            for task in self._processing_tasks:
                task.cancel()
            await asyncio.gather(*self._processing_tasks, return_exceptions=True)

        if self.subscription:
            try:
                await self.subscription.unsubscribe()
            except Exception as e:
                logger.warning(f"Error unsubscribing execution events consumer: {e}")

        if self._owns_nats_client:
            await self.nats_client.disconnect()

        logger.info("Execution events consumer stopped")

    async def _ensure_indexes(self) -> None:
        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.warning(
                "Execution events consumer: MongoDB unavailable; persistence will no-op"
            )
            return
        try:
            await self.db_manager.mongodb_adapter.ensure_indexes(
                EXECUTION_EVENTS_COLLECTION
            )
        except Exception as e:
            logger.warning(
                f"Failed to ensure indexes for {EXECUTION_EVENTS_COLLECTION}: {e}"
            )

    async def _on_message(self, msg: Any) -> None:
        try:
            await self._message_queue.put(msg)
        except asyncio.QueueFull:
            logger.warning("Execution events queue full; dropping message")
            execution_messages_failed.labels(error_type="queue_full").inc()

    async def _worker(self, worker_id: int) -> None:
        logger.info(f"Execution events worker {worker_id} started")
        try:
            while self.running:
                try:
                    msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                try:
                    await self._process_message(msg)
                except Exception as e:
                    logger.error(
                        f"Error in execution events worker {worker_id}: {e}",
                        exc_info=True,
                    )
                    execution_messages_failed.labels(error_type="processing").inc()
        except asyncio.CancelledError:
            pass
        logger.info(f"Execution events worker {worker_id} stopped")

    async def _process_message(self, msg: Any) -> None:
        start_time = asyncio.get_event_loop().time()
        subject = getattr(msg, "subject", self.subject)

        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Failed to decode execution event message: {e}")
            execution_messages_failed.labels(error_type="json_decode").inc()
            return

        execution_messages_received.labels(subject=subject).inc()

        with NATSTracePropagator.create_span_from_message(
            tracer,
            data,
            "persist_execution_event",
            span_kind=trace.SpanKind.CONSUMER,
        ) as span:
            span.set_attribute("messaging.destination", subject)

            event = ExecutionEvent.from_nats_message(data, subject=subject)
            if event is None:
                span.set_attribute("message.invalid", True)
                logger.warning(
                    "invalid_execution_event_received",
                    extra={"subject": subject, "raw_data": data},
                )
                execution_messages_failed.labels(error_type="invalid_payload").inc()
                return

            # Span attribute names match the cross-service identifier contract;
            # `execution.event_type` is consumed by the audit evaluator (FR22).
            span.set_attribute("decision.decision_id", event.decision_id)
            span.set_attribute("execution.event_type", event.event_type)
            span.set_attribute("execution.order_id", event.order_id)

            decision_attrs: dict[str, Any] = {
                "decision_id": event.decision_id,
                "strategy_id": event.strategy_id,
            }
            if event.symbol:
                decision_attrs["symbol"] = event.symbol
            embedded_ctx = extract_decision_context_from_nats(data)
            for k, v in embedded_ctx.items():
                decision_attrs.setdefault(k, v)
            set_decision_context(span, **decision_attrs)

            log_extra = {
                "subject": subject,
                "decision_id": event.decision_id,
                "strategy_id": event.strategy_id,
                "order_id": event.order_id,
                "event_type": event.event_type,
            }

            persisted = await self._persist(event)
            if persisted:
                execution_messages_persisted.inc()
                logger.info("execution_event_persisted", extra=log_extra)
                span.set_status(trace.Status(trace.StatusCode.OK))
            else:
                execution_messages_failed.labels(error_type="persistence").inc()
                logger.warning("execution_event_persistence_skipped", extra=log_extra)
                span.set_attribute("persistence.skipped", True)

            execution_processing_time.observe(
                asyncio.get_event_loop().time() - start_time
            )

    async def _persist(self, event: ExecutionEvent) -> bool:
        adapter = (
            getattr(self.db_manager, "mongodb_adapter", None)
            if self.db_manager
            else None
        )
        if adapter is None or getattr(adapter, "db", None) is None:
            return False
        try:
            doc = event.model_dump(exclude_none=True)
            # Unique key combines order_id + event_type — the same order produces
            # multiple events (placed, then filled, etc.) and each must persist.
            doc["_id"] = f"{event.order_id}:{event.event_type}"
            doc = adapter._prepare_for_bson(doc)
            try:
                from pymongo.errors import DuplicateKeyError
            except ImportError:  # pragma: no cover - pymongo ships with motor
                DuplicateKeyError = Exception  # type: ignore[assignment, misc]
            try:
                await adapter.db[EXECUTION_EVENTS_COLLECTION].insert_one(doc)
                return True
            except DuplicateKeyError:
                logger.debug(
                    "execution_event_already_persisted",
                    extra={
                        "order_id": event.order_id,
                        "event_type": event.event_type,
                    },
                )
                return True
        except Exception as e:
            logger.error(
                f"Failed to persist execution event "
                f"{event.order_id}:{event.event_type}: {e}"
            )
            return False
