"""CIO intent NATS consumer that persists messages into the `intents` collection (P0.2a)."""

import asyncio
import json
import logging
import time
from typing import Any

from opentelemetry import trace

try:
    from petrosa_otel import (
        extract_decision_context_from_nats,
        get_meter,
        set_decision_context,
    )
except ImportError:
    from opentelemetry import metrics as _otel_metrics

    def get_meter(name: str) -> Any:
        return _otel_metrics.get_meter(name)

    def extract_decision_context_from_nats(message_dict: Any) -> dict[str, Any]:
        return {}

    def set_decision_context(span: Any, **attributes: Any) -> None:
        return None


import constants
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.intent import IntentEvent
from data_manager.utils.nats_trace_propagator import NATSTracePropagator

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

INTENTS_COLLECTION = "intents"

_meter = get_meter(__name__)
_METRIC_ATTRS = {"service": constants.OTEL_SERVICE_NAME, "consumer": "intent"}

intent_messages_received = _meter.create_counter(
    "data_manager_intent_messages_received_total",
    description="Total intent messages received from NATS",
)
intent_messages_persisted = _meter.create_counter(
    "data_manager_intent_messages_persisted_total",
    description="Total intent messages successfully persisted",
)
intent_messages_failed = _meter.create_counter(
    "data_manager_intent_messages_failed_total",
    description="Total intent messages that failed processing",
)
intent_processing_time = _meter.create_histogram(
    "data_manager_intent_processing_seconds",
    description="Intent message processing time in seconds",
    unit="s",
)


class IntentConsumer:
    """Subscribes to `cio.intent.>` and persists each event to MongoDB `intents`.

    Wires OpenTelemetry decision context (decision_id, intent_id, strategy_id)
    onto each persistence span via petrosa-otel helpers, and emits structured
    log fields matching the cross-service identifier contract.
    """

    def __init__(
        self,
        nats_client: NATSClient | None = None,
        db_manager: Any | None = None,
        subject: str | None = None,
    ) -> None:
        self.nats_client = nats_client or NATSClient()
        self.db_manager = db_manager
        self.subject = subject or constants.NATS_INTENT_SUBJECT
        self.running = False
        self.subscription: Any = None
        self._message_queue: asyncio.Queue = asyncio.Queue(
            maxsize=constants.MESSAGE_QUEUE_SIZE
        )
        self._processing_tasks: list[asyncio.Task] = []
        self._owns_nats_client = nats_client is None

    async def start(self) -> bool:
        try:
            logger.info("Starting intent consumer", extra={"subject": self.subject})

            if self._owns_nats_client and not await self.nats_client.connect():
                logger.error("Failed to connect to NATS for intent consumer")
                return False

            await self._ensure_indexes()

            self.subscription = await self.nats_client.subscribe(
                subject=self.subject,
                callback=self._on_message,
            )
            if not self.subscription:
                logger.error(
                    "Failed to subscribe to intent subject",
                    extra={"subject": self.subject},
                )
                return False

            self.running = True
            workers = min(constants.MAX_CONCURRENT_TASKS, 5)
            for i in range(workers):
                self._processing_tasks.append(asyncio.create_task(self._worker(i)))

            logger.info(
                "Intent consumer started",
                extra={"subject": self.subject, "workers": workers},
            )
            return True
        except Exception as e:
            logger.error(f"Failed to start intent consumer: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        logger.info("Stopping intent consumer")
        self.running = False

        if self._processing_tasks:
            for task in self._processing_tasks:
                task.cancel()
            await asyncio.gather(*self._processing_tasks, return_exceptions=True)

        if self.subscription:
            try:
                await self.subscription.unsubscribe()
            except Exception as e:
                logger.warning(f"Error unsubscribing intent consumer: {e}")

        if self._owns_nats_client:
            await self.nats_client.disconnect()

        logger.info("Intent consumer stopped")

    async def _ensure_indexes(self) -> None:
        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.warning(
                "Intent consumer: MongoDB unavailable; persistence will no-op"
            )
            return
        try:
            await self.db_manager.mongodb_adapter.ensure_indexes(INTENTS_COLLECTION)
        except Exception as e:
            logger.warning(f"Failed to ensure indexes for {INTENTS_COLLECTION}: {e}")

    async def _on_message(self, msg: Any) -> None:
        try:
            await self._message_queue.put(msg)
        except asyncio.QueueFull:
            logger.warning("Intent queue full; dropping message")
            intent_messages_failed.add(1, {**_METRIC_ATTRS, "error_type": "queue_full"})

    async def _worker(self, worker_id: int) -> None:
        logger.info(f"Intent worker {worker_id} started")
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
                        f"Error in intent worker {worker_id}: {e}", exc_info=True
                    )
                    intent_messages_failed.add(
                        1, {**_METRIC_ATTRS, "error_type": "processing"}
                    )
        except asyncio.CancelledError:
            pass
        logger.info(f"Intent worker {worker_id} stopped")

    async def _process_message(self, msg: Any) -> None:
        # Use time.monotonic() instead of time.monotonic() — the
        # latter is deprecated in modern asyncio and silently fragile under
        # pytest-asyncio 1.x when called from sync helpers; see #178.
        start_time = time.monotonic()
        subject = getattr(msg, "subject", self.subject)

        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Failed to decode intent message: {e}")
            intent_messages_failed.add(
                1, {**_METRIC_ATTRS, "error_type": "json_decode"}
            )
            return

        intent_messages_received.add(1, {**_METRIC_ATTRS, "subject": subject})

        with NATSTracePropagator.create_span_from_message(
            tracer,
            data,
            "persist_intent_event",
            span_kind=trace.SpanKind.CONSUMER,
        ) as span:
            span.set_attribute("messaging.destination", subject)

            event = IntentEvent.from_nats_message(data, subject=subject)
            if event is None:
                span.set_attribute("message.invalid", True)
                logger.warning(
                    "invalid_intent_message_received",
                    extra={"subject": subject, "raw_data": data},
                )
                intent_messages_failed.add(
                    1, {**_METRIC_ATTRS, "error_type": "invalid_payload"}
                )
                return

            decision_attrs: dict[str, Any] = {
                "intent_id": event.intent_id,
                "strategy_id": event.strategy_id,
            }
            if event.decision_id:
                decision_attrs["decision_id"] = event.decision_id
            if event.symbol:
                decision_attrs["symbol"] = event.symbol
            if event.action:
                decision_attrs["action"] = event.action
            embedded_ctx = extract_decision_context_from_nats(data)
            for k, v in embedded_ctx.items():
                decision_attrs.setdefault(k, v)
            set_decision_context(span, **decision_attrs)

            log_extra = {
                "subject": subject,
                "decision_id": event.decision_id,
                "intent_id": event.intent_id,
                "strategy_id": event.strategy_id,
            }

            persisted = await self._persist(event)
            if persisted:
                intent_messages_persisted.add(1, _METRIC_ATTRS)
                logger.debug("intent_persisted", extra=log_extra)
                span.set_status(trace.Status(trace.StatusCode.OK))
            else:
                intent_messages_failed.add(
                    1, {**_METRIC_ATTRS, "error_type": "persistence"}
                )
                logger.warning("intent_persistence_skipped", extra=log_extra)
                span.set_attribute("persistence.skipped", True)

            intent_processing_time.record(time.monotonic() - start_time, _METRIC_ATTRS)

    async def _persist(self, event: IntentEvent) -> bool:
        adapter = (
            getattr(self.db_manager, "mongodb_adapter", None)
            if self.db_manager
            else None
        )
        if adapter is None or getattr(adapter, "db", None) is None:
            return False
        try:
            doc = event.model_dump(exclude_none=True)
            doc["_id"] = event.intent_id
            doc = adapter._prepare_for_bson(doc)
            try:
                from pymongo.errors import DuplicateKeyError
            except (
                ImportError
            ):  # pragma: no cover - pymongo always installed alongside motor
                DuplicateKeyError = Exception  # type: ignore[assignment, misc]
            try:
                await adapter.db[INTENTS_COLLECTION].insert_one(doc)
                return True
            except DuplicateKeyError:
                logger.debug(
                    "intent_already_persisted",
                    extra={"intent_id": event.intent_id},
                )
                return True
        except Exception as e:
            logger.error(f"Failed to persist intent {event.intent_id}: {e}")
            return False
