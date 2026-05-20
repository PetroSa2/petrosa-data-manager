"""Cross-subscriber integration test for the audit-trail chain (P0.2e).

Exercises the 3-leg round trip in-process:

    cio.intent.>           → IntentConsumer            → `intents`
    signals.trading.>      → DecisionConsumer          → `cio_decisions`
    execution.events.<sid> → ExecutionEventsConsumer   → `execution_events`

All three messages share a single `decision_id` and a single OTel trace
context so that:

  * the three persisted documents are joinable by `decision_id`
  * the three persistence spans live under a single trace
  * structured log records on each leg carry the `decision_id`

The 4-leg variant (`pnl.events.<sid>` → `pnl_events`) is deferred to P4.1
per the issue; when it ships, extend the simulator with a fourth publisher
and add a 4-leg assertion class.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import time
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

try:
    from datetime import UTC
except ImportError:  # pragma: no cover - py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017


# ---------------------------------------------------------------------------
# In-process simulator: fake NATS broker
# ---------------------------------------------------------------------------


class _FakeNATSMsg:
    __slots__ = ("subject", "data")

    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data


class _FakeSubscription:
    def __init__(self, broker: _FakeNATSBroker, subject: str) -> None:
        self._broker = broker
        self.subject = subject

    async def unsubscribe(self) -> None:
        self._broker.unsubscribe(self.subject)


class _FakeNATSBroker:
    """Minimal in-process NATS pub/sub with `>` wildcard subject matching.

    Supports just enough surface for the audit-trail consumers:
        - `subscribe(subject, callback)` registers a coroutine callback
        - `publish(subject, data)` synchronously dispatches to matching subs
        - `disconnect()` clears subscriptions

    `subject` patterns use NATS wildcard semantics: `cio.intent.>` matches
    `cio.intent.foo`, `cio.intent.foo.bar`, etc.
    """

    def __init__(self) -> None:
        self._subs: dict[str, Any] = {}  # subject_pattern -> callback

    @staticmethod
    def _matches(pattern: str, subject: str) -> bool:
        # Translate NATS `>` to fnmatch `*` for trailing-wildcard match.
        if pattern.endswith(".>"):
            prefix = pattern[: -len(".>")]
            return subject == prefix or subject.startswith(prefix + ".")
        return fnmatch.fnmatchcase(subject, pattern)

    def subscribe_sync(self, subject: str, callback: Any) -> _FakeSubscription:
        self._subs[subject] = callback
        return _FakeSubscription(self, subject)

    def unsubscribe(self, subject: str) -> None:
        self._subs.pop(subject, None)

    async def publish(self, subject: str, payload_bytes: bytes) -> None:
        for pattern, callback in list(self._subs.items()):
            if self._matches(pattern, subject):
                await callback(_FakeNATSMsg(subject=subject, data=payload_bytes))


class _FakeNATSClient:
    """Adapter exposing the NATSClient surface the consumers use.

    The consumers call `await self.nats_client.subscribe(subject=..., callback=...)`
    and `await self.nats_client.disconnect()`. The broker routes messages.
    """

    def __init__(self, broker: _FakeNATSBroker) -> None:
        self._broker = broker
        self.is_connected = True

    async def connect(self) -> bool:
        return True

    async def subscribe(self, subject: str, callback: Any) -> _FakeSubscription:
        return self._broker.subscribe_sync(subject, callback)

    async def disconnect(self) -> None:
        return None


# ---------------------------------------------------------------------------
# In-process simulator: fake Mongo
# ---------------------------------------------------------------------------


class _FakeMongoCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        # Mimic _id uniqueness — the consumers tolerate DuplicateKeyError but
        # never trigger it in the audit-trail happy path.
        existing_id = doc.get("_id")
        if existing_id is not None and any(
            d.get("_id") == existing_id for d in self.docs
        ):
            try:
                from pymongo.errors import DuplicateKeyError
            except ImportError:  # pragma: no cover
                DuplicateKeyError = Exception  # type: ignore[assignment, misc]
            raise DuplicateKeyError("duplicate _id")
        self.docs.append(dict(doc))

        class _InsertOneResult:
            inserted_id = existing_id

        return _InsertOneResult()


class _FakeMongoDB:
    def __init__(self) -> None:
        self._collections: dict[str, _FakeMongoCollection] = {}

    def __getitem__(self, name: str) -> _FakeMongoCollection:
        if name not in self._collections:
            self._collections[name] = _FakeMongoCollection()
        return self._collections[name]


class _FakeMongoAdapter:
    def __init__(self) -> None:
        self.db = _FakeMongoDB()

    async def ensure_indexes(self, _collection_name: str) -> None:
        return None

    def _prepare_for_bson(self, doc: dict[str, Any]) -> dict[str, Any]:
        return doc


class _FakeDBManager:
    def __init__(self) -> None:
        self.mongodb_adapter = _FakeMongoAdapter()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_tracer_provider(monkeypatch):
    """Install a real TracerProvider with an InMemorySpanExporter for the test.

    The repo-wide `conftest.py` sets `OTEL_SDK_DISABLED=true`, which makes
    the SDK's TracerProvider return NoOpTracers. We unset that env var
    before instantiating the provider so spans are actually recorded, then
    swap it into the global slot for the duration of the test.

    `tests/test_nats_trace_propagator.py` calls the Once-guarded
    `set_tracer_provider`, which causes each ProxyTracer in the consumer
    modules to cache a stale `_real_tracer`. Bypass that staleness by
    replacing the consumer modules' module-level `tracer` attributes with
    fresh tracers bound to this fixture's provider for the test duration.
    """
    from data_manager.consumer import (
        decision_consumer as dc_mod,
        execution_events_consumer as ec_mod,
        intent_consumer as ic_mod,
    )

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "test-cross-subscriber"})
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    previous = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = provider

    saved_tracers = {
        ic_mod: ic_mod.tracer,
        dc_mod: dc_mod.tracer,
        ec_mod: ec_mod.tracer,
    }
    for mod in saved_tracers:
        mod.tracer = provider.get_tracer(mod.__name__)  # type: ignore[attr-defined]

    try:
        yield exporter, provider
    finally:
        for mod, tracer in saved_tracers.items():
            mod.tracer = tracer  # type: ignore[attr-defined]
        provider.shutdown()
        trace._TRACER_PROVIDER = previous


@pytest.fixture
def broker() -> _FakeNATSBroker:
    return _FakeNATSBroker()


@pytest.fixture
def db_manager() -> _FakeDBManager:
    return _FakeDBManager()


@pytest_asyncio.fixture
async def started_consumers(broker, db_manager):
    """Start all three subscribers against the shared broker + db manager."""
    from data_manager.consumer.decision_consumer import DecisionConsumer
    from data_manager.consumer.execution_events_consumer import (
        ExecutionEventsConsumer,
    )
    from data_manager.consumer.intent_consumer import IntentConsumer

    nats = _FakeNATSClient(broker)
    intent = IntentConsumer(
        nats_client=nats,  # type: ignore[arg-type]
        db_manager=db_manager,
        subject="cio.intent.>",
    )
    decision = DecisionConsumer(
        nats_client=nats,  # type: ignore[arg-type]
        db_manager=db_manager,
        subject="signals.trading.>",
    )
    execution = ExecutionEventsConsumer(
        nats_client=nats,  # type: ignore[arg-type]
        db_manager=db_manager,
        subject="execution.events.>",
    )

    # Skip the consumer's own .connect() — the FakeNATSClient is always up,
    # and the consumers' worker loops pull from an internal queue that we
    # bypass by invoking _process_message directly in the broker callback.
    for consumer in (intent, decision, execution):
        consumer._owns_nats_client = False

    # Wire subscriptions: bypass the consumer's worker queue and dispatch the
    # message straight into `_process_message`. The worker indirection adds
    # no semantic value for this test and complicates teardown ordering.
    intent.subscription = await nats.subscribe(
        subject=intent.subject, callback=intent._process_message
    )
    decision.subscription = await nats.subscribe(
        subject=decision.subject, callback=decision._process_message
    )
    execution.subscription = await nats.subscribe(
        subject=execution.subject, callback=execution._process_message
    )

    yield intent, decision, execution

    for consumer in (intent, decision, execution):
        with contextlib.suppress(Exception):
            await consumer.subscription.unsubscribe()


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC).isoformat()

DECISION_ID = "dec_20260520T120000000_p02e"
INTENT_ID = "int_20260520T120000000_p02e"
STRATEGY_ID = "strat_mean_rev_p02e"
ORDER_ID = "ord_20260520T120000000_p02e"
SYMBOL = "BTCUSDT"


def _intent_payload(trace_headers: dict[str, str]) -> dict[str, Any]:
    return {
        "intent_id": INTENT_ID,
        "strategy_id": STRATEGY_ID,
        "decision_id": DECISION_ID,
        "timestamp": _NOW,
        "symbol": SYMBOL,
        "action": "buy",
        "confidence": 0.72,
        "_otel_trace_headers": dict(trace_headers),
    }


def _decision_payload(trace_headers: dict[str, str]) -> dict[str, Any]:
    return {
        "decision_id": DECISION_ID,
        "strategy_id": STRATEGY_ID,
        "timestamp": _NOW,
        "symbol": SYMBOL,
        "action": "buy",
        "price": 50_000.0,
        "quantity": 0.5,
        "source": "petrosa-cio",
        "metadata": {
            "cio_justification": "trend aligned + volatility within band",
            "correlation_id": "corr_p02e",
        },
        "_otel_trace_headers": dict(trace_headers),
    }


def _execution_payload(trace_headers: dict[str, str]) -> dict[str, Any]:
    return {
        "decision_id": DECISION_ID,
        "strategy_id": STRATEGY_ID,
        "order_id": ORDER_ID,
        "event_type": "filled",
        "timestamp": _NOW,
        "symbol": SYMBOL,
        "side": "buy",
        "qty": 0.5,
        "fill_qty": 0.5,
        "price": 50_010.0,
        "_otel_trace_headers": dict(trace_headers),
    }


async def _publish_three_leg_chain(
    broker: _FakeNATSBroker, tracer: trace.Tracer
) -> dict[str, str]:
    """Publish the three audit-trail events in publisher order under one trace.

    Returns the trace headers used so the test can assert the producer-side
    `traceparent` matches what landed on the persistence spans.
    """
    import json

    from data_manager.utils.nats_trace_propagator import NATSTracePropagator

    with tracer.start_as_current_span("simulator.audit_trail_chain") as root:
        # Capture the headers that any child publisher would inject.
        headers: dict[str, str] = {}
        with tracer.start_as_current_span("simulator.publish_intent"):
            stub: dict[str, Any] = {}
            NATSTracePropagator.inject_context(stub)
            headers = stub.get(NATSTracePropagator.TRACE_HEADERS_FIELD, {})
            await broker.publish(
                "cio.intent.trading",
                json.dumps(_intent_payload(headers)).encode(),
            )
        with tracer.start_as_current_span("simulator.publish_decision"):
            stub = {}
            NATSTracePropagator.inject_context(stub)
            headers = stub.get(NATSTracePropagator.TRACE_HEADERS_FIELD, {})
            await broker.publish(
                f"signals.trading.{SYMBOL}",
                json.dumps(_decision_payload(headers)).encode(),
            )
        with tracer.start_as_current_span("simulator.publish_execution"):
            stub = {}
            NATSTracePropagator.inject_context(stub)
            headers = stub.get(NATSTracePropagator.TRACE_HEADERS_FIELD, {})
            await broker.publish(
                f"execution.events.{STRATEGY_ID}",
                json.dumps(_execution_payload(headers)).encode(),
            )
        return {
            "trace_id_hex": format(root.get_span_context().trace_id, "032x"),
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_leg_round_trip_joins_by_decision_id(
    in_memory_tracer_provider, broker, db_manager, started_consumers
):
    """The three audit-trail collections each receive one doc, joined by decision_id."""
    _exporter, provider = in_memory_tracer_provider
    tracer = provider.get_tracer("test")

    started = time.monotonic()
    await _publish_three_leg_chain(broker, tracer)
    # Allow any pending tasks to drain (publish dispatches synchronously, so
    # this is a no-op in the happy path).
    await asyncio.sleep(0)
    elapsed = time.monotonic() - started

    intents = db_manager.mongodb_adapter.db["intents"].docs
    decisions = db_manager.mongodb_adapter.db["cio_decisions"].docs
    executions = db_manager.mongodb_adapter.db["execution_events"].docs

    assert len(intents) == 1, intents
    assert len(decisions) == 1, decisions
    assert len(executions) == 1, executions

    assert intents[0]["decision_id"] == DECISION_ID
    assert decisions[0]["decision_id"] == DECISION_ID
    assert executions[0]["decision_id"] == DECISION_ID

    # Cross-collection identity holds: strategy_id propagates through every leg.
    assert intents[0]["strategy_id"] == STRATEGY_ID
    assert decisions[0]["strategy_id"] == STRATEGY_ID
    assert executions[0]["strategy_id"] == STRATEGY_ID

    # Per-collection identity: each subscriber writes the expected primary key.
    assert intents[0]["_id"] == INTENT_ID
    assert decisions[0]["_id"] == DECISION_ID
    assert executions[0]["_id"] == f"{ORDER_ID}:filled"

    # Round-trip latency requirement from the acceptance criteria.
    assert elapsed < 5.0, f"round-trip took {elapsed:.2f}s"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_leg_trace_continuity_single_trace_id(
    in_memory_tracer_provider, broker, db_manager, started_consumers
):
    """All three persistence spans share the publisher's trace_id."""
    exporter, provider = in_memory_tracer_provider
    tracer = provider.get_tracer("test")

    out = await _publish_three_leg_chain(broker, tracer)
    await asyncio.sleep(0)

    spans = exporter.get_finished_spans()
    persistence_span_names = {
        "persist_intent_event",
        "persist_decision_event",
        "persist_execution_event",
    }
    persistence_spans = [s for s in spans if s.name in persistence_span_names]
    span_names_seen = {s.name for s in persistence_spans}

    # Each subscriber must have produced exactly one persistence span.
    assert span_names_seen == persistence_span_names, span_names_seen
    assert len(persistence_spans) == 3, [s.name for s in persistence_spans]

    trace_ids = {
        format(s.get_span_context().trace_id, "032x") for s in persistence_spans
    }
    assert len(trace_ids) == 1, trace_ids
    # The producer's root trace_id must equal the persistence-span trace_id.
    assert trace_ids == {out["trace_id_hex"]}, (trace_ids, out["trace_id_hex"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_leg_logs_carry_decision_id(
    in_memory_tracer_provider, broker, db_manager, started_consumers, caplog
):
    """Each subscriber's persistence log line carries the `decision_id`.

    Stand-in for the Loki cross-service join — when the records hit Loki,
    `{decision_id="X"}` returns one line from each subscriber.
    """
    _exporter, provider = in_memory_tracer_provider
    tracer = provider.get_tracer("test")

    caplog.set_level(logging.DEBUG, logger="data_manager.consumer.intent_consumer")
    caplog.set_level(logging.DEBUG, logger="data_manager.consumer.decision_consumer")
    caplog.set_level(
        logging.DEBUG, logger="data_manager.consumer.execution_events_consumer"
    )

    await _publish_three_leg_chain(broker, tracer)
    await asyncio.sleep(0)

    persisted_records = [
        r
        for r in caplog.records
        if r.name.startswith("data_manager.consumer.")
        and getattr(r, "decision_id", None) == DECISION_ID
    ]
    # Expect exactly one record per leg carrying decision_id.
    by_logger = {r.name for r in persisted_records}
    assert by_logger == {
        "data_manager.consumer.intent_consumer",
        "data_manager.consumer.decision_consumer",
        "data_manager.consumer.execution_events_consumer",
    }, by_logger
