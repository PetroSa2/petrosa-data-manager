"""JetStream consumer for LLM gateway call records (petrosa-data-manager#841, supervisor decision A).

The stream ``LLM_CALL_RECORDS`` (subject ``petrosa.llm.call_record.v1``) is owned by infra
(petrosa_k8s#1532/#1535). data-manager **checks that it exists and never creates it**: when it is missing,
the consumer logs an ERROR and re-checks every ``STREAM_RECHECK_SECONDS``.

Both data-manager pods bind the **same durable pull consumer**, so JetStream hands each message to one pod
(no double processing). Per message:

- valid record, stored (or already stored: a redelivery) -> ``ack``;
- invalid payload (the contract rejects it) -> ``term``: it can never become valid, so it is not redelivered;
- database error -> ``nak`` with a delay growing with the delivery count; on the last delivery
  (``MAX_DELIVER``) a WARNING ``LLM_CALL_RECORD_DELIVERY_EXHAUSTED`` and
  ``data_manager_llm_call_records_consumed_total{result="exhausted"}`` make the lost record visible.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from data_manager.consumer.nats_client import NATSClient
from data_manager.services import llm_call_records

try:
    from petrosa_otel import get_meter
except ImportError:  # pragma: no cover - petrosa_otel always present in prod
    from opentelemetry import metrics as _otel_metrics

    get_meter = _otel_metrics.get_meter

logger = logging.getLogger(__name__)

STREAM = os.getenv("LLM_CALL_RECORDS_STREAM", "LLM_CALL_RECORDS")
SUBJECT = os.getenv("LLM_CALL_RECORDS_SUBJECT", "petrosa.llm.call_record.v1")
DURABLE = os.getenv("LLM_CALL_RECORDS_DURABLE", "data-manager-llm-call-records")
#: Decision A's consumer parameters (labelled; overridable).
ACK_WAIT_SECONDS = float(os.getenv("LLM_CALL_RECORDS_ACK_WAIT_SECONDS", "30"))
MAX_DELIVER = int(os.getenv("LLM_CALL_RECORDS_MAX_DELIVER", "10"))
FETCH_BATCH = int(os.getenv("LLM_CALL_RECORDS_FETCH_BATCH", "50"))
FETCH_TIMEOUT_SECONDS = float(os.getenv("LLM_CALL_RECORDS_FETCH_TIMEOUT_SECONDS", "5"))
STREAM_RECHECK_SECONDS = float(
    os.getenv("LLM_CALL_RECORDS_STREAM_RECHECK_SECONDS", "60")
)
NAK_BASE_DELAY_SECONDS = 2.0
NAK_MAX_DELAY_SECONDS = 60.0

_meter = get_meter(__name__)
CONSUMED = _meter.create_counter(
    "data_manager_llm_call_records_consumed_total",
    description="LLM call record messages by result (stored | duplicate | invalid | retry | exhausted)",
)


def nak_delay(num_delivered: int) -> float:
    """Exponential backoff for a redelivery after a database error, capped."""
    return min(
        NAK_MAX_DELAY_SECONDS, NAK_BASE_DELAY_SECONDS * 2 ** max(0, num_delivered - 1)
    )


class LlmCallRecordConsumer:
    """Durable pull consumer on ``LLM_CALL_RECORDS``; acknowledges only after the Mongo write."""

    def __init__(
        self,
        db_manager: Any,
        nats_client: NATSClient | None = None,
        store: llm_call_records.LlmCallRecordStore | None = None,
    ) -> None:
        self.db_manager = db_manager
        self._owns_nats_client = nats_client is None
        self.nats_client = nats_client or NATSClient()
        self.store = store
        self.subscription: Any = None
        self.running = False
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    # -- message handling ----------------------------------------------------------------------
    async def handle(self, msg: Any) -> str:
        """Process one message; returns the result label."""
        delivered = int(
            getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
        )
        try:
            record = llm_call_records.parse_record(msg.data)
        except llm_call_records.InvalidLlmCallRecord as exc:
            logger.warning("LLM_CALL_RECORD_INVALID: %s", str(exc)[:300])
            await msg.term()
            return self._count("invalid")
        try:
            stored = await self.store.save(record)  # type: ignore[union-attr]
        except Exception as exc:
            if delivered >= MAX_DELIVER:
                logger.warning(
                    "LLM_CALL_RECORD_DELIVERY_EXHAUSTED request_id=%s attempt_no=%s "
                    "deliveries=%d: %s: %s",
                    record.request_id,
                    record.attempt_no,
                    delivered,
                    type(exc).__name__,
                    exc,
                )
                await msg.term()
                return self._count("exhausted")
            await msg.nak(delay=nak_delay(delivered))
            return self._count("retry")
        await msg.ack()
        return self._count("stored" if stored else "duplicate")

    @staticmethod
    def _count(result: str) -> str:
        CONSUMED.add(1, {"result": result})
        return result

    # -- lifecycle -------------------------------------------------------------------------------
    async def _stream_exists(self, js: Any) -> bool:
        try:
            await js.stream_info(STREAM)
            return True
        except Exception as exc:
            logger.error(
                "LLM_CALL_RECORDS_STREAM_MISSING stream=%s (%s: %s); data-manager never creates it "
                "(infra owns it); re-checking in %.0f s",
                STREAM,
                type(exc).__name__,
                exc,
                STREAM_RECHECK_SECONDS,
            )
            return False

    async def _bind(self) -> bool:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        js = self.nats_client.nc.jetstream()
        if not await self._stream_exists(js):
            return False
        self.subscription = await js.pull_subscribe(
            SUBJECT,
            durable=DURABLE,
            stream=STREAM,
            config=ConsumerConfig(
                durable_name=DURABLE,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=ACK_WAIT_SECONDS,
                max_deliver=MAX_DELIVER,
                deliver_policy=DeliverPolicy.ALL,
            ),
        )
        logger.info(
            "LLM call record consumer bound: stream=%s durable=%s", STREAM, DURABLE
        )
        return True

    async def _run(self) -> None:
        import nats.errors

        while self.running and not await self._bind():
            await asyncio.sleep(STREAM_RECHECK_SECONDS)
        while self.running:
            try:
                messages = await self.subscription.fetch(
                    FETCH_BATCH, timeout=FETCH_TIMEOUT_SECONDS
                )
            except (TimeoutError, nats.errors.TimeoutError):
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "llm call record fetch failed: %s: %s", type(exc).__name__, exc
                )
                await asyncio.sleep(5)
                continue
            for msg in messages:
                await self.handle(msg)

    async def start(self) -> bool:
        if self._owns_nats_client and not await self.nats_client.connect():
            logger.error("Failed to connect to NATS for the LLM call record consumer")
            return False
        if self.store is None:
            self.store = llm_call_records.LlmCallRecordStore(
                self.db_manager.mongodb_adapter
            )
        self.running = True
        self._task = asyncio.create_task(self._run(), name="llm-call-record-consumer")
        return True

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.subscription is not None:
            try:
                await self.subscription.unsubscribe()
            except Exception:
                pass
        if self._owns_nats_client:
            await self.nats_client.disconnect()
