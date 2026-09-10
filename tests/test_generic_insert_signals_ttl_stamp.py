"""
Regression tests for POST /api/v1/{database}/{collection} (generic.py::insert_records)
— the `_ttl_inserted_at` stamp for MongoDB `signals` inserts.

Companion to petrosa-bot-ta-analysis#267 AC6: signals are persisted by callers
that already supply their own `timestamp` as an ISO *string* (JSON-compat
requirement), so the existing "add timestamp if not present" auto-stamp never
fires for them, and a TTL index keyed on that string field would never expire
anything (the same gotcha already observed on the `alerts` collection).
`insert_records` therefore stamps an UNCONDITIONAL, real BSON ``Date`` field
`_ttl_inserted_at` specifically on `mongodb.signals` inserts, which
`data_manager.maintenance.intents_ttl_index.ensure_signals_ttl_index` TTL-indexes.
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.db.mysql_adapter import WriteResult


@pytest.fixture
def client(mock_db_manager):
    """Create test client with a mocked Mongo adapter capturing write() calls."""
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)

    mock_db_manager.mysql_adapter = Mock()
    mock_db_manager.mysql_adapter.write = Mock(
        return_value=WriteResult(inserted=1, duplicates=0, failed=0)
    )

    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


def test_signals_insert_gets_ttl_inserted_at_stamp_even_with_client_timestamp(client):
    """The core regression: a signal payload that ALREADY includes its own
    string `timestamp` (as ta_bot always sends) must still get a real BSON
    Date `_ttl_inserted_at` field added, so the TTL index has something to
    key on."""
    response = client.post(
        "/api/v1/mongodb/signals",
        json={
            "data": {
                "symbol": "BTCUSDT",
                "action": "buy",
                "confidence": 0.85,
                "timestamp": "2026-09-02T20:36:31Z",  # client-supplied string
            }
        },
    )

    assert response.status_code == 200
    api_module.db_manager.mongodb_adapter.write.assert_called_once()
    (model_instances, collection), _kwargs = (
        api_module.db_manager.mongodb_adapter.write.call_args
    )
    assert collection == "signals"
    assert len(model_instances) == 1
    inserted = model_instances[0].model_dump()

    # The client's own string timestamp must be preserved untouched...
    assert inserted["timestamp"] == "2026-09-02T20:36:31Z"
    # ...AND a dedicated real datetime field must be present for TTL to key on.
    assert "_ttl_inserted_at" in inserted
    assert isinstance(inserted["_ttl_inserted_at"], datetime)


def test_signals_batch_insert_stamps_every_item(client):
    """Batch inserts (persist_signals_batch) must stamp every item, not just
    the first."""
    response = client.post(
        "/api/v1/mongodb/signals",
        json={
            "data": [
                {
                    "symbol": "BTCUSDT",
                    "action": "buy",
                    "timestamp": "2026-09-02T00:00:00Z",
                },
                {
                    "symbol": "ETHUSDT",
                    "action": "sell",
                    "timestamp": "2026-09-02T00:01:00Z",
                },
            ]
        },
    )

    assert response.status_code == 200
    (model_instances, _collection), _kwargs = (
        api_module.db_manager.mongodb_adapter.write.call_args
    )
    assert len(model_instances) == 2
    for instance in model_instances:
        dumped = instance.model_dump()
        assert isinstance(dumped["_ttl_inserted_at"], datetime)


def test_non_signals_mongo_collection_is_not_stamped(client):
    """The stamp is scoped to `signals` only — other Mongo collections must
    not be affected by this change."""
    response = client.post(
        "/api/v1/mongodb/alerts",
        json={"data": {"symbol": "BTCUSDT", "message": "test"}},
    )

    assert response.status_code == 200
    (model_instances, collection), _kwargs = (
        api_module.db_manager.mongodb_adapter.write.call_args
    )
    assert collection == "alerts"
    assert "_ttl_inserted_at" not in model_instances[0].model_dump()


def test_mysql_signals_insert_is_not_stamped(client):
    """The stamp is Mongo-only — the (legacy, being phased out) MySQL signals
    fallback path must be untouched."""
    response = client.post(
        "/api/v1/mysql/signals",
        json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
    )

    assert response.status_code == 200
    api_module.db_manager.mysql_adapter.write.assert_called_once()
    model_instances, _collection = api_module.db_manager.mysql_adapter.write.call_args[
        0
    ]
    assert "_ttl_inserted_at" not in model_instances[0].model_dump()
