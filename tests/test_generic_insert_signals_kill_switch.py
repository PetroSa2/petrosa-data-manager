"""
Regression tests for POST /api/v1/{database}/{collection} (generic.py::insert_records)
— the `PETROSA_SIGNALS_PERSIST_ENABLED` kill-switch for MongoDB `signals` inserts.

data-manager#302: live Atlas re-query confirmed the `signals` Mongo collection
has no confirmed reader anywhere in the 8-repo ecosystem. Mirrors the `alerts`
kill-switch (`data_manager.services.alert_dispatcher._persist_enabled`,
data-manager#271 AC7) so the operator can stop the write immediately via
config + restart with no code change, without touching dispatch/webhook
behavior elsewhere or any other collection.
"""

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


def test_signals_persist_enabled_defaults_to_true(monkeypatch):
    from data_manager.api.routes.generic import _signals_persist_enabled

    monkeypatch.delenv("PETROSA_SIGNALS_PERSIST_ENABLED", raising=False)
    assert _signals_persist_enabled() is True


def test_signals_persist_enabled_false_when_env_false(monkeypatch):
    from data_manager.api.routes.generic import _signals_persist_enabled

    monkeypatch.setenv("PETROSA_SIGNALS_PERSIST_ENABLED", "false")
    assert _signals_persist_enabled() is False


def test_signals_insert_skipped_when_kill_switch_disabled(client, monkeypatch):
    """With the kill-switch off, the Mongo adapter must never be called for
    `mongodb.signals`, and the caller gets a graceful zero-insert response
    (not an error) — ta_bot's `persist_signal` treats `inserted_count == 0`
    as a soft failure, not an exception."""
    monkeypatch.setenv("PETROSA_SIGNALS_PERSIST_ENABLED", "false")

    response = client.post(
        "/api/v1/mongodb/signals",
        json={
            "data": {
                "symbol": "BTCUSDT",
                "action": "buy",
                "timestamp": "2026-09-02T20:36:31Z",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["inserted_count"] == 0
    assert "disabled" in body["message"].lower()
    api_module.db_manager.mongodb_adapter.write.assert_not_called()


def test_signals_insert_proceeds_when_kill_switch_enabled(client, monkeypatch):
    """Default (unset) and explicit 'true' must both persist normally —
    the kill-switch is opt-out, not opt-in."""
    monkeypatch.delenv("PETROSA_SIGNALS_PERSIST_ENABLED", raising=False)

    response = client.post(
        "/api/v1/mongodb/signals",
        json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
    )

    assert response.status_code == 200
    assert response.json()["inserted_count"] == 1
    api_module.db_manager.mongodb_adapter.write.assert_called_once()


def test_kill_switch_does_not_affect_other_mongo_collections(client, monkeypatch):
    """The gate is scoped to mongodb.signals only — alerts must be
    unaffected even when the signals kill-switch is disabled."""
    monkeypatch.setenv("PETROSA_SIGNALS_PERSIST_ENABLED", "false")

    response = client.post(
        "/api/v1/mongodb/alerts",
        json={"data": {"symbol": "BTCUSDT", "message": "test"}},
    )

    assert response.status_code == 200
    assert response.json()["inserted_count"] == 1
    api_module.db_manager.mongodb_adapter.write.assert_called_once()


def test_kill_switch_does_not_affect_mysql_signals_path(client, monkeypatch):
    """The gate is Mongo-only — the legacy MySQL `signals` path (writer
    already removed per petrosa-bot-ta-analysis#284) is untouched."""
    monkeypatch.setenv("PETROSA_SIGNALS_PERSIST_ENABLED", "false")

    response = client.post(
        "/api/v1/mysql/signals",
        json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
    )

    assert response.status_code == 200
    api_module.db_manager.mysql_adapter.write.assert_called_once()


def test_signals_batch_insert_skipped_when_kill_switch_disabled(client, monkeypatch):
    """Batch inserts must also be gated — not just single-record inserts."""
    monkeypatch.setenv("PETROSA_SIGNALS_PERSIST_ENABLED", "false")

    response = client.post(
        "/api/v1/mongodb/signals",
        json={
            "data": [
                {"symbol": "BTCUSDT", "action": "buy"},
                {"symbol": "ETHUSDT", "action": "sell"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["inserted_count"] == 0
    api_module.db_manager.mongodb_adapter.write.assert_not_called()
