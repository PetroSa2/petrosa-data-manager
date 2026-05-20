"""Tests for the pnl events consumer (P0.2d, #141 / umbrella #140).

Mirrors `test_execution_events_consumer.py` so the four audit-trail
subscribers are tested with the same matrix. The publisher side of P0.2d
lands with P4.1 P&L computation; until then this consumer no-ops on the
wire, but its parsing, model validation, persistence path, and OTel
context propagation are fully exercised here.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.consumer.nats_client import NATSClient
from data_manager.consumer.pnl_consumer import (
    PNL_EVENTS_COLLECTION,
    PnlConsumer,
)
from data_manager.models.pnl_event import PnlEvent

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
def pnl_consumer(mock_nats_client_async, mock_db_manager):
    return PnlConsumer(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        subject="pnl.events.>",
    )


def _pnl_payload(**overrides):
    base = {
        "decision_id": "dec_20260518T120000000_xyz789",
        "strategy_id": "strat_momentum_v1",
        "timestamp": "2026-05-18T12:00:05+00:00",
        "pnl_kind": "closed",
        "realized_pnl_usd": 12.5,
        "order_id": "ord_abc123",
        "currency": "USD",
        "extra_field": "preserved",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_pnl_event_parses_valid_payload():
    data = _pnl_payload()
    event = PnlEvent.from_nats_message(data, subject="pnl.events.strat_momentum_v1")
    assert event is not None
    assert event.decision_id == "dec_20260518T120000000_xyz789"
    assert event.strategy_id == "strat_momentum_v1"
    assert event.pnl_kind == "closed"
    assert event.realized_pnl_usd == 12.5
    assert event.order_id == "ord_abc123"
    assert event.currency == "USD"
    assert event.subject == "pnl.events.strat_momentum_v1"
    assert event.payload == {"extra_field": "preserved"}


def test_pnl_event_accepts_kind_alias():
    """Publisher may send `kind` rather than `pnl_kind` — both are accepted."""
    data = _pnl_payload()
    del data["pnl_kind"]
    data["kind"] = "mark_to_market"
    data["unrealized_pnl_usd"] = 3.14
    event = PnlEvent.from_nats_message(data)
    assert event is not None
    assert event.pnl_kind == "mark_to_market"
    assert event.unrealized_pnl_usd == 3.14


def test_pnl_event_rejects_missing_required_fields():
    assert PnlEvent.from_nats_message({"decision_id": "d"}) is None
    assert PnlEvent.from_nats_message({"decision_id": "d", "strategy_id": "s"}) is None
    assert (
        PnlEvent.from_nats_message(
            {"decision_id": "d", "strategy_id": "s", "pnl_kind": "closed"}
        )
        is None
    )


def test_pnl_event_rejects_invalid_timestamp():
    bad = _pnl_payload(timestamp="not-a-date")
    assert PnlEvent.from_nats_message(bad) is None


def test_pnl_event_accepts_numeric_timestamp():
    ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    event = PnlEvent.from_nats_message(_pnl_payload(timestamp=ts.timestamp()))
    assert event is not None
    assert event.timestamp == ts


def test_pnl_event_strips_trace_and_decision_context():
    data = _pnl_payload(
        _trace_context={"traceparent": "00-..."},
        _otel_trace_headers={"traceparent": "00-..."},
        _decision_context={"strategy_id": "x"},
    )
    event = PnlEvent.from_nats_message(data)
    assert event is not None
    assert "_trace_context" not in event.payload
    assert "_otel_trace_headers" not in event.payload
    assert "_decision_context" not in event.payload


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


def _build_msg(payload, subject="pnl.events.strat_momentum_v1"):
    msg = MagicMock()
    msg.data.decode.return_value = json.dumps(payload)
    msg.subject = subject
    return msg


@pytest.mark.asyncio
async def test_process_message_persists_pnl(pnl_consumer, mock_db_manager):
    msg = _build_msg(_pnl_payload())
    await pnl_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_awaited_once()
    inserted = collection.insert_one.await_args.args[0]
    assert inserted["decision_id"] == "dec_20260518T120000000_xyz789"
    assert inserted["pnl_kind"] == "closed"
    # The synthesized _id composes decision_id + pnl_kind + timestamp microseconds.
    assert inserted["_id"].startswith("dec_20260518T120000000_xyz789:closed:")


@pytest.mark.asyncio
async def test_process_message_skips_invalid_payload(pnl_consumer, mock_db_manager):
    msg = _build_msg({"only": "garbage"})
    await pnl_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_handles_invalid_json(pnl_consumer, mock_db_manager):
    msg = MagicMock()
    msg.data.decode.return_value = "{not-json}"
    msg.subject = "pnl.events.strat_momentum_v1"
    await pnl_consumer._process_message(msg)
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_sets_decision_context_attrs(pnl_consumer):
    msg = _build_msg(_pnl_payload(pnl_kind="mark_to_market"))
    with patch("data_manager.consumer.pnl_consumer.set_decision_context") as mock_set:
        await pnl_consumer._process_message(msg)
    assert mock_set.called
    _, kwargs = mock_set.call_args
    assert kwargs["decision_id"] == "dec_20260518T120000000_xyz789"
    assert kwargs["strategy_id"] == "strat_momentum_v1"


@pytest.mark.asyncio
async def test_persist_tolerates_duplicate_key(pnl_consumer, mock_db_manager):
    collection = mock_db_manager.mongodb_adapter.db.__getitem__.return_value
    try:
        from pymongo.errors import DuplicateKeyError
    except ImportError:
        DuplicateKeyError = Exception  # type: ignore[assignment, misc]
    collection.insert_one.side_effect = DuplicateKeyError("dup")
    event = PnlEvent.from_nats_message(_pnl_payload())
    assert event is not None
    assert await pnl_consumer._persist(event) is True


@pytest.mark.asyncio
async def test_persist_returns_false_without_adapter(pnl_consumer):
    pnl_consumer.db_manager = None
    event = PnlEvent.from_nats_message(_pnl_payload())
    assert event is not None
    assert await pnl_consumer._persist(event) is False


@pytest.mark.asyncio
async def test_start_subscribes_to_configured_subject(
    pnl_consumer, mock_nats_client_async, mock_db_manager
):
    mock_nats_client_async.subscribe.return_value = MagicMock()
    pnl_consumer._owns_nats_client = False  # don't drive connect()
    started = await pnl_consumer.start()
    assert started is True
    assert (
        mock_nats_client_async.subscribe.await_args.kwargs["subject"] == "pnl.events.>"
    )
    mock_db_manager.mongodb_adapter.ensure_indexes.assert_awaited_once_with(
        PNL_EVENTS_COLLECTION
    )
    await pnl_consumer.stop()


@pytest.mark.asyncio
async def test_start_returns_false_when_subscribe_fails(
    pnl_consumer, mock_nats_client_async
):
    mock_nats_client_async.subscribe.return_value = None
    pnl_consumer._owns_nats_client = False
    started = await pnl_consumer.start()
    assert started is False
