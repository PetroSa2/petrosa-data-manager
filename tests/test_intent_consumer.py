"""Tests for the CIO intent consumer (P0.2a)."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.consumer.intent_consumer import (
    INTENTS_COLLECTION,
    IntentConsumer,
)
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.intent import IntentEvent

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
def intent_consumer(mock_nats_client_async, mock_db_manager):
    return IntentConsumer(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        subject="cio.intent.>",
    )


def _intent_payload(**overrides):
    base = {
        "intent_id": "int_20260518T120000000_abc123",
        "strategy_id": "strat_mean_rev",
        "timestamp": "2026-05-18T12:00:00+00:00",
        "decision_id": None,
        "symbol": "BTCUSDT",
        "action": "buy",
        "confidence": 0.72,
        "extra_field": "preserved",
    }
    base.update(overrides)
    return base


def test_intent_event_parses_valid_payload():
    data = _intent_payload(decision_id="dec_20260518T120000000_xyz789")
    event = IntentEvent.from_nats_message(data, subject="cio.intent.trading")
    assert event is not None
    assert event.intent_id == "int_20260518T120000000_abc123"
    assert event.strategy_id == "strat_mean_rev"
    assert event.decision_id == "dec_20260518T120000000_xyz789"
    assert event.symbol == "BTCUSDT"
    assert event.action == "buy"
    assert event.confidence == 0.72
    assert event.subject == "cio.intent.trading"
    assert event.payload == {"extra_field": "preserved"}


def test_intent_event_rejects_missing_required_fields():
    assert IntentEvent.from_nats_message({"intent_id": "x"}) is None
    assert IntentEvent.from_nats_message({"strategy_id": "s"}) is None
    assert IntentEvent.from_nats_message({"intent_id": "x", "strategy_id": "s"}) is None


def test_intent_event_rejects_invalid_timestamp():
    bad = _intent_payload(timestamp="not-a-date")
    assert IntentEvent.from_nats_message(bad) is None


def test_intent_event_accepts_numeric_timestamp():
    ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    epoch = ts.timestamp()
    event = IntentEvent.from_nats_message(_intent_payload(timestamp=epoch))
    assert event is not None
    assert event.timestamp == ts


def test_intent_event_strips_trace_and_decision_context():
    data = _intent_payload(
        _trace_context={"traceparent": "00-..."},
        _decision_context={"strategy_id": "x"},
    )
    event = IntentEvent.from_nats_message(data)
    assert event is not None
    assert "_trace_context" not in event.payload
    assert "_decision_context" not in event.payload


def _build_msg(payload, subject="cio.intent.trading"):
    msg = MagicMock()
    msg.data.decode.return_value = json.dumps(payload)
    msg.subject = subject
    return msg


@pytest.mark.asyncio
async def test_process_message_persists_intent(intent_consumer, mock_db_manager):
    msg = _build_msg(_intent_payload(decision_id="dec_abc"))
    await intent_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_awaited_once()
    inserted = collection.insert_one.await_args.args[0]
    assert inserted["_id"] == "int_20260518T120000000_abc123"
    assert inserted["intent_id"] == "int_20260518T120000000_abc123"
    assert inserted["decision_id"] == "dec_abc"
    assert inserted["strategy_id"] == "strat_mean_rev"


@pytest.mark.asyncio
async def test_process_message_skips_invalid_payload(intent_consumer, mock_db_manager):
    msg = _build_msg({"only": "garbage"})
    await intent_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_handles_invalid_json(intent_consumer, mock_db_manager):
    msg = MagicMock()
    msg.data.decode.return_value = "{not-json}"
    msg.subject = "cio.intent.trading"
    await intent_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_sets_decision_context_attrs(intent_consumer):
    msg = _build_msg(_intent_payload(decision_id="dec_xyz"))
    with patch(
        "data_manager.consumer.intent_consumer.set_decision_context"
    ) as mock_set:
        await intent_consumer._process_message(msg)
    assert mock_set.called
    _, kwargs = mock_set.call_args
    assert kwargs["intent_id"] == "int_20260518T120000000_abc123"
    assert kwargs["strategy_id"] == "strat_mean_rev"
    assert kwargs["decision_id"] == "dec_xyz"
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["action"] == "buy"


@pytest.mark.asyncio
async def test_persist_tolerates_duplicate_key(intent_consumer, mock_db_manager):
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    try:
        from pymongo.errors import DuplicateKeyError
    except ImportError:
        DuplicateKeyError = Exception  # type: ignore[assignment, misc]
    collection.insert_one.side_effect = DuplicateKeyError("dup")
    event = IntentEvent.from_nats_message(_intent_payload())
    assert event is not None
    assert await intent_consumer._persist(event) is True


@pytest.mark.asyncio
async def test_persist_returns_false_without_adapter(intent_consumer):
    intent_consumer.db_manager = None
    event = IntentEvent.from_nats_message(_intent_payload())
    assert event is not None
    assert await intent_consumer._persist(event) is False


@pytest.mark.asyncio
async def test_start_subscribes_to_configured_subject(
    intent_consumer, mock_nats_client_async, mock_db_manager
):
    mock_nats_client_async.subscribe.return_value = MagicMock()
    intent_consumer._owns_nats_client = False  # don't drive connect()
    started = await intent_consumer.start()
    assert started is True
    assert (
        mock_nats_client_async.subscribe.await_args.kwargs["subject"] == "cio.intent.>"
    )
    mock_db_manager.mongodb_adapter.ensure_indexes.assert_awaited_once_with(
        INTENTS_COLLECTION
    )
    await intent_consumer.stop()


@pytest.mark.asyncio
async def test_start_returns_false_when_subscribe_fails(
    intent_consumer, mock_nats_client_async
):
    mock_nats_client_async.subscribe.return_value = None
    intent_consumer._owns_nats_client = False
    started = await intent_consumer.start()
    assert started is False
