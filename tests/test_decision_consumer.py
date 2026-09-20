"""Tests for the CIO decision consumer (P0.2b)."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.consumer.decision_consumer import (
    CIO_DECISIONS_COLLECTION,
    DecisionConsumer,
    _cio_decisions_mysql_persist_enabled,
)
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.decision import DecisionEvent

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
    db_manager.mysql_adapter = MagicMock()
    return db_manager


@pytest.fixture
def decision_consumer(mock_nats_client_async, mock_db_manager):
    return DecisionConsumer(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        subject="signals.trading.>",
    )


def _decision_payload(**overrides):
    base = {
        "decision_id": "dec_20260518T120000000_xyz789",
        "strategy_id": "strat_momentum_v1",
        "timestamp": "2026-05-18T12:00:00+00:00",
        "symbol": "BTCUSDT",
        "action": "buy",
        "price": 65000.0,
        "current_price": 65000.0,
        "quantity": 0.001,
        "confidence": 0.9,
        "source": "petrosa-cio",
        "metadata": {
            "correlation_id": "corr_abc",
            "cio_justification": "Momentum + RSI confluence",
            "thought_trace": [{"step": "momentum_check", "verdict": "bullish"}],
        },
        "extra_field": "preserved",
    }
    base.update(overrides)
    return base


def test_decision_event_parses_valid_payload():
    data = _decision_payload()
    event = DecisionEvent.from_nats_message(
        data, subject="signals.trading.strat_momentum_v1"
    )
    assert event is not None
    assert event.decision_id == "dec_20260518T120000000_xyz789"
    assert event.strategy_id == "strat_momentum_v1"
    assert event.symbol == "BTCUSDT"
    assert event.action == "buy"
    assert event.price == 65000.0
    assert event.quantity == 0.001
    assert event.confidence == 0.9
    assert event.source == "petrosa-cio"
    assert event.subject == "signals.trading.strat_momentum_v1"
    assert event.reasoning["correlation_id"] == "corr_abc"
    assert event.reasoning["cio_justification"] == "Momentum + RSI confluence"
    assert event.reasoning["thought_trace"][0]["verdict"] == "bullish"
    assert event.payload == {"current_price": 65000.0, "extra_field": "preserved"}


def test_decision_event_accepts_strategy_alias():
    data = _decision_payload()
    data.pop("strategy_id")
    data["strategy"] = "strat_momentum_v1"
    event = DecisionEvent.from_nats_message(data)
    assert event is not None
    assert event.strategy_id == "strat_momentum_v1"


def test_decision_event_rejects_missing_required_fields():
    assert DecisionEvent.from_nats_message({"decision_id": "x"}) is None
    assert DecisionEvent.from_nats_message({"strategy_id": "s"}) is None
    assert (
        DecisionEvent.from_nats_message({"decision_id": "x", "strategy_id": "s"})
        is None
    )


def test_decision_event_rejects_invalid_timestamp():
    bad = _decision_payload(timestamp="not-a-date")
    assert DecisionEvent.from_nats_message(bad) is None


def test_decision_event_accepts_numeric_timestamp():
    ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    epoch = ts.timestamp()
    event = DecisionEvent.from_nats_message(_decision_payload(timestamp=epoch))
    assert event is not None
    assert event.timestamp == ts


def test_decision_event_strips_trace_and_decision_context():
    data = _decision_payload(
        _trace_context={"traceparent": "00-..."},
        _decision_context={"strategy_id": "x"},
    )
    event = DecisionEvent.from_nats_message(data)
    assert event is not None
    assert "_trace_context" not in event.payload
    assert "_decision_context" not in event.payload


def test_decision_event_handles_missing_metadata():
    data = _decision_payload()
    data.pop("metadata")
    event = DecisionEvent.from_nats_message(data)
    assert event is not None
    assert event.reasoning == {}


def _build_msg(payload, subject="signals.trading.strat_momentum_v1"):
    msg = MagicMock()
    msg.data.decode.return_value = json.dumps(payload)
    msg.subject = subject
    return msg


@pytest.mark.asyncio
async def test_process_message_persists_decision(decision_consumer, mock_db_manager):
    msg = _build_msg(_decision_payload())
    await decision_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_awaited_once()
    inserted = collection.insert_one.await_args.args[0]
    assert inserted["_id"] == "dec_20260518T120000000_xyz789"
    assert inserted["decision_id"] == "dec_20260518T120000000_xyz789"
    assert inserted["strategy_id"] == "strat_momentum_v1"
    assert inserted["action"] == "buy"
    assert inserted["reasoning"]["cio_justification"] == "Momentum + RSI confluence"


@pytest.mark.asyncio
async def test_process_message_skips_invalid_payload(
    decision_consumer, mock_db_manager
):
    msg = _build_msg({"only": "garbage"})
    await decision_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_handles_invalid_json(decision_consumer, mock_db_manager):
    msg = MagicMock()
    msg.data.decode.return_value = "{not-json}"
    msg.subject = "signals.trading.strat_momentum_v1"
    await decision_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_sets_decision_context_attrs(decision_consumer):
    msg = _build_msg(_decision_payload())
    with patch(
        "data_manager.consumer.decision_consumer.set_decision_context"
    ) as mock_set:
        await decision_consumer._process_message(msg)
    assert mock_set.called
    _, kwargs = mock_set.call_args
    assert kwargs["decision_id"] == "dec_20260518T120000000_xyz789"
    assert kwargs["strategy_id"] == "strat_momentum_v1"
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["action"] == "buy"


@pytest.mark.asyncio
async def test_persist_tolerates_duplicate_key(decision_consumer, mock_db_manager):
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    try:
        from pymongo.errors import DuplicateKeyError
    except ImportError:
        DuplicateKeyError = Exception  # type: ignore[assignment, misc]
    collection.insert_one.side_effect = DuplicateKeyError("dup")
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None
    assert await decision_consumer._persist(event) is True


@pytest.mark.asyncio
async def test_persist_returns_false_without_adapter(decision_consumer):
    decision_consumer.db_manager = None
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None
    assert await decision_consumer._persist(event) is False


@pytest.mark.asyncio
async def test_start_subscribes_to_configured_subject(
    decision_consumer, mock_nats_client_async, mock_db_manager
):
    mock_nats_client_async.subscribe.return_value = MagicMock()
    decision_consumer._owns_nats_client = False  # don't drive connect()
    started = await decision_consumer.start()
    assert started is True
    assert (
        mock_nats_client_async.subscribe.await_args.kwargs["subject"]
        == "signals.trading.>"
    )
    mock_db_manager.mongodb_adapter.ensure_indexes.assert_awaited_once_with(
        CIO_DECISIONS_COLLECTION
    )
    await decision_consumer.stop()


@pytest.mark.asyncio
async def test_start_returns_false_when_subscribe_fails(
    decision_consumer, mock_nats_client_async
):
    mock_nats_client_async.subscribe.return_value = None
    decision_consumer._owns_nats_client = False
    started = await decision_consumer.start()
    assert started is False


# --------------------------------------------------------------------------- #
# MySQL cio_decisions dual-write (2026-09-20) — MySQL is the unbounded
# permanent copy since Mongo cio_decisions was cut to a 1-day TTL.
# --------------------------------------------------------------------------- #


def test_cio_decisions_mysql_persist_enabled_defaults_to_true(monkeypatch):
    monkeypatch.delenv("PETROSA_CIO_DECISIONS_MYSQL_PERSIST_ENABLED", raising=False)
    assert _cio_decisions_mysql_persist_enabled() is True


def test_cio_decisions_mysql_persist_enabled_false_when_env_false(monkeypatch):
    monkeypatch.setenv("PETROSA_CIO_DECISIONS_MYSQL_PERSIST_ENABLED", "false")
    assert _cio_decisions_mysql_persist_enabled() is False


@pytest.mark.asyncio
async def test_persist_dual_writes_to_mysql(decision_consumer, mock_db_manager):
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None

    assert await decision_consumer._persist(event) is True

    mock_db_manager.mysql_adapter.write.assert_called_once()
    records, collection = mock_db_manager.mysql_adapter.write.call_args[0]
    assert collection == CIO_DECISIONS_COLLECTION
    assert records == [event]


@pytest.mark.asyncio
async def test_persist_skips_mysql_when_kill_switch_disabled(
    decision_consumer, mock_db_manager, monkeypatch
):
    monkeypatch.setenv("PETROSA_CIO_DECISIONS_MYSQL_PERSIST_ENABLED", "false")
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None

    # Mongo write must still happen — the switches are independent.
    assert await decision_consumer._persist(event) is True
    mock_db_manager.mysql_adapter.write.assert_not_called()
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_mysql_failure_does_not_block_mongo_write(
    decision_consumer, mock_db_manager
):
    mock_db_manager.mysql_adapter.write.side_effect = RuntimeError("mysql down")
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None

    assert await decision_consumer._persist(event) is True
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_without_mysql_adapter_does_not_raise(
    decision_consumer, mock_db_manager
):
    mock_db_manager.mysql_adapter = None
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None

    assert await decision_consumer._persist(event) is True


@pytest.mark.asyncio
async def test_persist_dual_writes_even_without_db_manager_mongo_adapter(
    decision_consumer,
):
    """MySQL and Mongo persistence are independent: if db_manager itself is
    unset, `_dual_write_mysql` must no-op quietly rather than raise, and
    `_persist` still returns False (Mongo unavailable) without blowing up."""
    decision_consumer.db_manager = None
    event = DecisionEvent.from_nats_message(_decision_payload())
    assert event is not None

    assert await decision_consumer._persist(event) is False
