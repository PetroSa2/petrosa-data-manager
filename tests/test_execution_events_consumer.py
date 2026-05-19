"""Tests for the execution events consumer (P0.2c)."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.consumer.execution_events_consumer import (
    EXECUTION_EVENTS_COLLECTION,
    ExecutionEventsConsumer,
)
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.execution_event import ExecutionEvent

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc  # noqa: UP017


@pytest.fixture
def mock_nats_client_async():
    return AsyncMock(spec=NATSClient)


@pytest.fixture
def mock_db_manager():
    db_manager = MagicMock()
    mongo = MagicMock()
    mongo.ensure_indexes = AsyncMock()
    mongo._prepare_for_bson = lambda d: d
    mongo.db = MagicMock()
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    mongo.db.__getitem__.return_value = collection
    db_manager.mongodb_adapter = mongo
    return db_manager


@pytest.fixture
def execution_events_consumer(mock_nats_client_async, mock_db_manager):
    return ExecutionEventsConsumer(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        subject="execution.events.>",
    )


def _exec_payload(**overrides):
    base = {
        "decision_id": "dec_20260518T120000000_xyz789",
        "strategy_id": "strat_momentum_v1",
        "order_id": "ord_abc123",
        "event_type": "filled",
        "timestamp": "2026-05-18T12:00:01+00:00",
        "reason": "fully filled at market",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": 0.001,
        "fill_qty": 0.001,
        "price": 65010.0,
        "exchange_order_id": "binance-9988",
    }
    base.update(overrides)
    return base


def test_execution_event_parses_valid_payload():
    data = _exec_payload()
    event = ExecutionEvent.from_nats_message(
        data, subject="execution.events.strat_momentum_v1"
    )
    assert event is not None
    assert event.decision_id == "dec_20260518T120000000_xyz789"
    assert event.strategy_id == "strat_momentum_v1"
    assert event.order_id == "ord_abc123"
    assert event.event_type == "filled"
    assert event.symbol == "BTCUSDT"
    assert event.side == "buy"
    assert event.qty == 0.001
    assert event.fill_qty == 0.001
    assert event.price == 65010.0
    assert event.reason == "fully filled at market"
    assert event.subject == "execution.events.strat_momentum_v1"
    assert event.payload == {"exchange_order_id": "binance-9988"}


def test_execution_event_accepts_strategy_alias():
    data = _exec_payload()
    data.pop("strategy_id")
    data["strategy"] = "strat_momentum_v1"
    event = ExecutionEvent.from_nats_message(data)
    assert event is not None
    assert event.strategy_id == "strat_momentum_v1"


def test_execution_event_rejects_missing_required_fields():
    assert ExecutionEvent.from_nats_message({"decision_id": "x"}) is None
    base = _exec_payload()
    for field in ("decision_id", "strategy_id", "order_id", "event_type", "timestamp"):
        payload = dict(base)
        payload.pop(field)
        assert ExecutionEvent.from_nats_message(payload) is None, (
            f"Expected None when {field} missing"
        )


def test_execution_event_rejects_invalid_timestamp():
    bad = _exec_payload(timestamp="not-a-date")
    assert ExecutionEvent.from_nats_message(bad) is None


def test_execution_event_accepts_numeric_timestamp():
    ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    epoch = ts.timestamp()
    event = ExecutionEvent.from_nats_message(_exec_payload(timestamp=epoch))
    assert event is not None
    assert event.timestamp == ts


def test_execution_event_strips_trace_and_decision_context():
    data = _exec_payload(
        _trace_context={"traceparent": "00-..."},
        _decision_context={"strategy_id": "x"},
        _otel_trace_headers={"traceparent": "00-..."},
    )
    event = ExecutionEvent.from_nats_message(data)
    assert event is not None
    assert "_trace_context" not in event.payload
    assert "_decision_context" not in event.payload
    assert "_otel_trace_headers" not in event.payload


def _build_msg(payload, subject="execution.events.strat_momentum_v1"):
    msg = MagicMock()
    msg.data.decode.return_value = json.dumps(payload)
    msg.subject = subject
    return msg


@pytest.mark.asyncio
async def test_process_message_persists_execution_event(
    execution_events_consumer, mock_db_manager
):
    msg = _build_msg(_exec_payload())
    await execution_events_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_awaited_once()
    inserted = collection.insert_one.await_args.args[0]
    # Synthesized _id keeps multi-event-per-order persistence idempotent.
    assert inserted["_id"] == "ord_abc123:filled"
    assert inserted["decision_id"] == "dec_20260518T120000000_xyz789"
    assert inserted["order_id"] == "ord_abc123"
    assert inserted["event_type"] == "filled"
    assert inserted["fill_qty"] == 0.001


@pytest.mark.asyncio
async def test_process_message_skips_invalid_payload(
    execution_events_consumer, mock_db_manager
):
    msg = _build_msg({"only": "garbage"})
    await execution_events_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_handles_invalid_json(
    execution_events_consumer, mock_db_manager
):
    msg = MagicMock()
    msg.data.decode.return_value = "{not-json}"
    msg.subject = "execution.events.strat_momentum_v1"
    await execution_events_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_sets_decision_context_attrs(execution_events_consumer):
    msg = _build_msg(_exec_payload())
    with patch(
        "data_manager.consumer.execution_events_consumer.set_decision_context"
    ) as mock_set:
        await execution_events_consumer._process_message(msg)
    assert mock_set.called
    _, kwargs = mock_set.call_args
    assert kwargs["decision_id"] == "dec_20260518T120000000_xyz789"
    assert kwargs["strategy_id"] == "strat_momentum_v1"
    assert kwargs["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_persist_tolerates_duplicate_key(
    execution_events_consumer, mock_db_manager
):
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    try:
        from pymongo.errors import DuplicateKeyError
    except ImportError:
        DuplicateKeyError = Exception  # type: ignore[assignment, misc]
    collection.insert_one.side_effect = DuplicateKeyError("dup")
    event = ExecutionEvent.from_nats_message(_exec_payload())
    assert event is not None
    assert await execution_events_consumer._persist(event) is True


@pytest.mark.asyncio
async def test_persist_returns_false_without_adapter(execution_events_consumer):
    execution_events_consumer.db_manager = None
    event = ExecutionEvent.from_nats_message(_exec_payload())
    assert event is not None
    assert await execution_events_consumer._persist(event) is False


@pytest.mark.asyncio
async def test_start_subscribes_to_configured_subject(
    execution_events_consumer, mock_nats_client_async, mock_db_manager
):
    mock_nats_client_async.subscribe.return_value = MagicMock()
    execution_events_consumer._owns_nats_client = False
    started = await execution_events_consumer.start()
    assert started is True
    assert (
        mock_nats_client_async.subscribe.await_args.kwargs["subject"]
        == "execution.events.>"
    )
    mock_db_manager.mongodb_adapter.ensure_indexes.assert_awaited_once_with(
        EXECUTION_EVENTS_COLLECTION
    )
    await execution_events_consumer.stop()


@pytest.mark.asyncio
async def test_start_returns_false_when_subscribe_fails(
    execution_events_consumer, mock_nats_client_async
):
    mock_nats_client_async.subscribe.return_value = None
    execution_events_consumer._owns_nats_client = False
    started = await execution_events_consumer.start()
    assert started is False


def test_default_subject_uses_constants_topic_env(monkeypatch):
    """Subscriber MUST read the prefix from NATS_TOPIC_EXECUTION_EVENTS."""
    # The constant is resolved at import time, so assert the default form.
    import constants

    assert constants.NATS_TOPIC_EXECUTION_EVENTS == "execution.events"
    assert constants.NATS_EXECUTION_EVENTS_SUBJECT.endswith(".>")
    assert constants.NATS_EXECUTION_EVENTS_SUBJECT.startswith(
        constants.NATS_TOPIC_EXECUTION_EVENTS
    )


@pytest.mark.asyncio
async def test_process_message_sets_otel_span_attributes(execution_events_consumer):
    """Span MUST carry decision.decision_id + execution.event_type per AC."""
    msg = _build_msg(_exec_payload())
    captured = {}

    class _FakeSpan:
        def set_attribute(self, k, v):
            captured[k] = v

        def set_status(self, *_a, **_kw):
            pass

    class _FakeCtxMgr:
        def __enter__(self_inner):
            return _FakeSpan()

        def __exit__(self_inner, *a):
            return False

    with patch(
        "data_manager.consumer.execution_events_consumer.NATSTracePropagator.create_span_from_message",
        return_value=_FakeCtxMgr(),
    ):
        await execution_events_consumer._process_message(msg)

    assert captured.get("decision.decision_id") == "dec_20260518T120000000_xyz789"
    assert captured.get("execution.event_type") == "filled"
    assert captured.get("execution.order_id") == "ord_abc123"
