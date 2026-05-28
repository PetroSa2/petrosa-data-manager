"""Tests for the alert spine (petrosa-data-manager#183).

Covers:

* `AlertEvent.from_nats_message` parsing — subject decomposition,
  fallback dedup keys, severity coercion, timestamp shapes.
* `AlertDispatcher._record_for_rate_limit` — sliding-window AC5 logic
  with both default and per-category-env limits.
* `AlertDispatcher.dispatch` end-to-end with a mocked Mongo adapter:
  accept-path, suppress-path, summary-rollup-path.
* `AlertDispatcher._attempt_delivery` — happy path, mock-delivery path
  (no webhook URL), bounded retry then `failed`, and retry-then-success.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.consumer.nats_client import NATSClient
from data_manager.models.alert import (
    AlertDeliveryState,
    AlertEvent,
    AlertSeverity,
)
from data_manager.services.alert_dispatcher import (
    ALERTS_COLLECTION,
    AlertDispatcher,
)

try:
    from datetime import UTC
except ImportError:  # py310
    UTC = timezone.utc  # noqa: UP017


# ---------------------------------------------------------------------------
# AlertEvent.from_nats_message
# ---------------------------------------------------------------------------


def _body(**overrides: Any) -> dict[str, Any]:
    base = {
        "severity": "warning",
        "subsystem": "tradeengine",
        "message": "reconciliation mismatch on pos-123",
        "decision_id": "dec_test_001",
        "timestamp": "2026-05-28T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_alert_event_parses_subject_dedupe_token():
    event = AlertEvent.from_nats_message(
        "alerts.position.reconciliation.mismatch.pos-123",
        _body(),
    )
    assert event is not None
    assert event.category == "position.reconciliation.mismatch"
    assert event.dedupe_key == "pos-123"
    assert event.severity == AlertSeverity.WARNING
    assert event.decision_id == "dec_test_001"
    assert event.subsystem == "tradeengine"


def test_alert_event_honours_explicit_dedupe_key():
    """When body carries `dedupe_key`, the WHOLE subject suffix is category."""
    event = AlertEvent.from_nats_message(
        "alerts.position.reconciliation.mismatch.pos-999",
        _body(dedupe_key="canonical-key-xyz"),
    )
    assert event is not None
    assert event.dedupe_key == "canonical-key-xyz"
    assert event.category == "position.reconciliation.mismatch.pos-999"


def test_alert_event_single_segment_subject_uses_decision_id():
    """`alerts.<single>` falls back to decision_id as dedupe key."""
    event = AlertEvent.from_nats_message("alerts.backup_failed", _body())
    assert event is not None
    assert event.category == "backup_failed"
    assert event.dedupe_key == "dec_test_001"


def test_alert_event_falls_back_to_message_hash_when_no_id():
    body = _body()
    body.pop("decision_id")
    event = AlertEvent.from_nats_message("alerts.backup_failed", body)
    assert event is not None
    assert event.dedupe_key  # non-empty; deterministic hash
    # Same body → same hash (deterministic dedup)
    event_again = AlertEvent.from_nats_message("alerts.backup_failed", body)
    assert event_again.dedupe_key == event.dedupe_key


def test_alert_event_severity_coercion():
    event = AlertEvent.from_nats_message("alerts.cat.token", _body(severity="critical"))
    assert event is not None
    assert event.severity == AlertSeverity.CRITICAL


def test_alert_event_invalid_severity_defaults_to_warning():
    event = AlertEvent.from_nats_message("alerts.cat.token", _body(severity="garbled"))
    assert event is not None
    assert event.severity == AlertSeverity.WARNING


def test_alert_event_rejects_non_alert_subject():
    assert AlertEvent.from_nats_message("metrics.cpu", _body()) is None
    assert AlertEvent.from_nats_message("", _body()) is None


def test_alert_event_id_is_deterministic():
    event = AlertEvent.from_nats_message(
        "alerts.position.reconciliation.mismatch.pos-7", _body()
    )
    assert event is not None
    # The ID must be stable across two builds of the same event.
    event2 = AlertEvent.from_nats_message(
        "alerts.position.reconciliation.mismatch.pos-7", _body()
    )
    assert event.make_id() == event2.make_id()
    assert "position.reconciliation.mismatch" in event.make_id()
    assert "pos-7" in event.make_id()


def test_alert_event_timestamp_int_becomes_utc_datetime():
    event = AlertEvent.from_nats_message(
        "alerts.cat.token", _body(timestamp=1716897600)
    )
    assert event is not None
    assert event.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# AlertDispatcher fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nats_client_async():
    nats = AsyncMock(spec=NATSClient)
    nats.connect = AsyncMock(return_value=True)
    nats.disconnect = AsyncMock()
    return nats


@pytest.fixture
def mock_db_manager():
    db_manager = MagicMock()
    mongo = MagicMock()
    mongo.ensure_indexes = AsyncMock()
    mongo._prepare_for_bson = lambda d: d
    mongo.db = MagicMock()
    collection = MagicMock()
    collection.replace_one = AsyncMock()
    mongo.db.__getitem__.return_value = collection
    db_manager.mongodb_adapter = mongo
    return db_manager


@pytest.fixture
def dispatcher(mock_nats_client_async, mock_db_manager):
    return AlertDispatcher(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        # Default: no webhook URL → mock mode.
        webhook_url="",
    )


# ---------------------------------------------------------------------------
# AC5 — rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_accepts_under_default_limit(dispatcher):
    # Default = 10/min; first 10 should accept, 11th suppresses.
    accepted = 0
    suppressed = 0
    for i in range(15):
        event = AlertEvent.from_nats_message(
            "alerts.test.cat.id" + str(i), _body(decision_id=f"d{i}")
        )
        assert event is not None
        result = dispatcher._record_for_rate_limit(event)
        if result == "accept":
            accepted += 1
        else:
            suppressed += 1
    assert accepted == 10
    assert suppressed == 5


def test_rate_limiter_uses_per_category_env(dispatcher, monkeypatch):
    """`PETROSA_ALERT_RATELIMIT_<CATEGORY>` overrides the default."""
    monkeypatch.setenv("PETROSA_ALERT_RATELIMIT_BACKUP_FAILED", "3")
    accepted = 0
    for i in range(5):
        event = AlertEvent.from_nats_message(
            "alerts.backup_failed", _body(decision_id=f"d{i}")
        )
        assert event is not None
        if dispatcher._record_for_rate_limit(event) == "accept":
            accepted += 1
    assert accepted == 3


def test_rate_limiter_window_slides(dispatcher):
    """When time advances past the window, old timestamps drop and capacity recovers."""
    # Saturate the window
    for i in range(10):
        event = AlertEvent.from_nats_message(
            "alerts.cat.slide", _body(decision_id=f"d{i}")
        )
        dispatcher._record_for_rate_limit(event)
    # Advance virtual time by 61s so all prior timestamps are evicted
    now_after = dispatcher._now() + 61.0
    dispatcher._now = lambda: now_after
    event = AlertEvent.from_nats_message(
        "alerts.cat.slide", _body(decision_id="d-after-window")
    )
    assert dispatcher._record_for_rate_limit(event) == "accept"


# ---------------------------------------------------------------------------
# AC4 — persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_persists_with_deterministic_id(dispatcher, mock_db_manager):
    body = _body()
    event = await dispatcher.dispatch(
        subject="alerts.position.reconciliation.mismatch.pos-42", body=body
    )
    assert event is not None
    collection = mock_db_manager.mongodb_adapter.db[ALERTS_COLLECTION]
    collection.replace_one.assert_called()
    # The Mongo `_id` is the deterministic event ID.
    call = collection.replace_one.call_args_list[0]
    flt, doc = call.args[0], call.args[1]
    assert flt["_id"] == event.make_id()
    assert doc["_id"] == event.make_id()
    assert doc["category"] == "position.reconciliation.mismatch"
    assert doc["dedupe_key"] == "pos-42"


@pytest.mark.asyncio
async def test_dispatch_invalid_subject_returns_none(dispatcher):
    event = await dispatcher.dispatch(subject="metrics.cpu", body=_body())
    assert event is None


# ---------------------------------------------------------------------------
# AC3 — delivery (mock + retry + failed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_marks_mock_when_no_webhook_configured(
    dispatcher, mock_db_manager
):
    event = await dispatcher.dispatch(
        subject="alerts.position.mismatch.pos-1", body=_body()
    )
    assert event is not None
    assert event.delivery_state == AlertDeliveryState.DELIVERED_MOCK
    assert len(event.delivery_attempts) == 1
    assert event.delivery_attempts[0].state == AlertDeliveryState.DELIVERED_MOCK


@pytest.mark.asyncio
async def test_delivery_happy_path_with_webhook(
    mock_nats_client_async, mock_db_manager
):
    http_client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    http_client.post = AsyncMock(return_value=response)
    dispatcher = AlertDispatcher(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        webhook_url="https://example.invalid/hook",
        http_client=http_client,
    )
    event = await dispatcher.dispatch(
        subject="alerts.position.mismatch.pos-1", body=_body()
    )
    assert event is not None
    assert event.delivery_state == AlertDeliveryState.DELIVERED
    assert http_client.post.await_count == 1


@pytest.mark.asyncio
async def test_delivery_retries_then_marks_failed(
    mock_nats_client_async, mock_db_manager
):
    """All three attempts return 500 → state must end in `failed` with 3 attempts."""
    http_client = AsyncMock()
    response = MagicMock()
    response.status_code = 500
    http_client.post = AsyncMock(return_value=response)
    dispatcher = AlertDispatcher(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        webhook_url="https://example.invalid/hook",
        http_client=http_client,
    )

    # Patch sleep to keep the test under a second.
    with patch("data_manager.services.alert_dispatcher.asyncio.sleep", new=AsyncMock()):
        event = await dispatcher.dispatch(
            subject="alerts.position.mismatch.pos-fail", body=_body()
        )
    assert event is not None
    assert event.delivery_state == AlertDeliveryState.FAILED
    assert len(event.delivery_attempts) == 3
    assert event.delivery_attempts[-1].state == AlertDeliveryState.FAILED


@pytest.mark.asyncio
async def test_delivery_recovers_on_second_attempt(
    mock_nats_client_async, mock_db_manager
):
    """First attempt 503, second attempt 200 → `delivered` with 2 attempts."""
    http_client = AsyncMock()
    bad = MagicMock()
    bad.status_code = 503
    ok = MagicMock()
    ok.status_code = 200
    http_client.post = AsyncMock(side_effect=[bad, ok])
    dispatcher = AlertDispatcher(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        webhook_url="https://example.invalid/hook",
        http_client=http_client,
    )
    with patch("data_manager.services.alert_dispatcher.asyncio.sleep", new=AsyncMock()):
        event = await dispatcher.dispatch(
            subject="alerts.position.mismatch.pos-retry", body=_body()
        )
    assert event is not None
    assert event.delivery_state == AlertDeliveryState.DELIVERED
    # Two attempt rows: the failed retry then the successful delivery.
    assert len(event.delivery_attempts) == 2
    assert event.delivery_attempts[-1].state == AlertDeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_delivery_timeout_records_error_string(
    mock_nats_client_async, mock_db_manager
):
    import httpx

    http_client = AsyncMock()
    http_client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    dispatcher = AlertDispatcher(
        nats_client=mock_nats_client_async,
        db_manager=mock_db_manager,
        webhook_url="https://example.invalid/hook",
        http_client=http_client,
    )
    with patch("data_manager.services.alert_dispatcher.asyncio.sleep", new=AsyncMock()):
        event = await dispatcher.dispatch(
            subject="alerts.position.mismatch.pos-timeout", body=_body()
        )
    assert event is not None
    assert event.delivery_state == AlertDeliveryState.FAILED
    assert any(att.error == "timeout" for att in event.delivery_attempts)


# ---------------------------------------------------------------------------
# AC5 — suppressed + summary rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppressed_events_persist_but_not_delivered(dispatcher, mock_db_manager):
    # First fill the per-category bucket.
    for i in range(10):
        await dispatcher.dispatch(
            subject="alerts.spammy.cat.id" + str(i),
            body=_body(decision_id=f"d{i}"),
        )
    # The 11th event is over the limit → suppressed (state PENDING, attempt = suppressed)
    suppressed = await dispatcher.dispatch(
        subject="alerts.spammy.cat.id-over",
        body=_body(decision_id="d-over"),
    )
    assert suppressed is not None
    assert suppressed.delivery_state == AlertDeliveryState.PENDING
    assert any(
        att.error == "suppressed_by_rate_limit" for att in suppressed.delivery_attempts
    )


@pytest.mark.asyncio
async def test_summary_alert_emitted_when_suppression_count_hits_threshold(
    dispatcher, mock_db_manager
):
    # Saturate the bucket.
    for i in range(10):
        await dispatcher.dispatch(
            subject="alerts.flood.cat.id" + str(i),
            body=_body(decision_id=f"d{i}"),
        )
    # Suppress 10 more — should emit one summary on the 10th suppression.
    for i in range(10):
        await dispatcher.dispatch(
            subject="alerts.flood.cat.s" + str(i),
            body=_body(decision_id=f"s{i}"),
        )
    # Inspect the upserted documents — at least one is a summary alert.
    collection = mock_db_manager.mongodb_adapter.db[ALERTS_COLLECTION]
    summary_calls = [
        c
        for c in collection.replace_one.call_args_list
        if c.args[1].get("category", "").startswith("summary.")
    ]
    assert summary_calls, "Expected a summary alert after 10 suppressions"
    summary_doc = summary_calls[0].args[1]
    assert summary_doc["category"] == "summary.flood.cat"
    assert summary_doc["summarized_ids"]  # carries the suppressed event IDs


# ---------------------------------------------------------------------------
# Subscription glue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_subscribes_and_ensures_indexes(
    dispatcher, mock_nats_client_async, mock_db_manager
):
    mock_nats_client_async.subscribe = AsyncMock(return_value=MagicMock())
    ok = await dispatcher.start()
    assert ok is True
    mock_db_manager.mongodb_adapter.ensure_indexes.assert_awaited_with(
        ALERTS_COLLECTION
    )
    mock_nats_client_async.subscribe.assert_awaited()


@pytest.mark.asyncio
async def test_on_message_drops_malformed_json(dispatcher):
    msg = MagicMock()
    msg.subject = "alerts.cat.token"
    msg.data = b"not-json"
    # Should not raise.
    await dispatcher._on_message(msg)


@pytest.mark.asyncio
async def test_on_message_dispatches_valid_json(dispatcher, mock_db_manager):
    msg = MagicMock()
    msg.subject = "alerts.position.mismatch.pos-9"
    msg.data = json.dumps(_body()).encode()
    await dispatcher._on_message(msg)
    collection = mock_db_manager.mongodb_adapter.db[ALERTS_COLLECTION]
    collection.replace_one.assert_called()


# Silence pytest's unused-import linter — datetime is referenced in test bodies
_ = datetime
