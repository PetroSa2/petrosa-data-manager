"""
Regression tests for PUT /api/v1/{database}/{collection} (generic.py::update_records).

Covers petrosa-data-manager#262: `updated_count` was only ever assigned on the
empty-match/upsert-create branch, so any update targeting existing records
(the common case) raised UnboundLocalError -> HTTP 500. These tests exercise
both the previously-broken "matching_records non-empty" path and the
previously-working "empty-match/upsert-create" path to guard against
regression, plus the "no match / no upsert" no-op path.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.utils.circuit_breaker import CircuitBreakerOpenError


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked database adapters."""
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)
    mock_db_manager.mongodb_adapter.update = AsyncMock(return_value=1)

    mock_db_manager.mysql_adapter = Mock()
    mock_db_manager.mysql_adapter.query_range = Mock(return_value=[])
    mock_db_manager.mysql_adapter.write = Mock()
    mock_db_manager.mysql_adapter.update = Mock(return_value=1)

    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


def test_update_existing_record_mysql_returns_200_not_500(client):
    """The core regression: updating an EXISTING mysql.positions row must not 500."""
    client.app.state  # noqa: B018 - touch app to keep TestClient warm
    api_module.db_manager.mysql_adapter.query_range = Mock(
        return_value=[{"symbol": "BTCUSDT", "side": "SHORT", "quantity": 1.0}]
    )

    response = client.put(
        "/api/v1/mysql/positions",
        json={
            "filter": {"symbol": "BTCUSDT", "side": "SHORT"},
            "data": {"quantity": 2.0},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated_count"] == 1
    assert "Successfully updated 1 records" in body["message"]

    api_module.db_manager.mysql_adapter.update.assert_called_once()
    call_args = api_module.db_manager.mysql_adapter.update.call_args
    assert call_args.args[0] == "positions"
    assert call_args.args[1] == {"symbol": "BTCUSDT", "side": "SHORT"}
    assert call_args.args[2]["quantity"] == 2.0
    assert "updated_at" in call_args.args[2]


def test_update_existing_record_mongodb_returns_200(client):
    """Same non-empty-match path against the MongoDB adapter."""
    api_module.db_manager.mongodb_adapter.query_range = AsyncMock(
        return_value=[{"symbol": "BTCUSDT", "pnl": 10.0}]
    )

    response = client.put(
        "/api/v1/mongodb/daily_pnl",
        json={"filter": {"symbol": "BTCUSDT"}, "data": {"pnl": 25.0}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated_count"] == 1

    api_module.db_manager.mongodb_adapter.update.assert_called_once()


def test_update_no_match_no_upsert_is_noop(client):
    """No matching records and upsert=False: 200, updated_count=0, no write attempted."""
    api_module.db_manager.mysql_adapter.query_range = Mock(return_value=[])

    response = client.put(
        "/api/v1/mysql/positions",
        json={"filter": {"symbol": "NOPE"}, "data": {"quantity": 1.0}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated_count"] == 0
    assert body["message"] == "No records found matching filter"
    api_module.db_manager.mysql_adapter.update.assert_not_called()
    api_module.db_manager.mysql_adapter.write.assert_not_called()


def test_update_no_match_with_upsert_creates_record_mysql(client):
    """Empty-match/upsert-create path (previously the only working path) — no regression."""
    from data_manager.db.mysql_adapter import WriteResult

    api_module.db_manager.mysql_adapter.query_range = Mock(return_value=[])
    api_module.db_manager.mysql_adapter.write = Mock(
        return_value=WriteResult(inserted=1, duplicates=0, failed=0)
    )

    response = client.put(
        "/api/v1/mysql/positions",
        json={
            "filter": {"symbol": "ETHUSDT"},
            "data": {"symbol": "ETHUSDT", "quantity": 3.0},
            "upsert": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated_count"] == 1
    api_module.db_manager.mysql_adapter.write.assert_called_once()
    api_module.db_manager.mysql_adapter.update.assert_not_called()


def test_update_no_match_with_upsert_creates_record_mongodb(client):
    """Empty-match/upsert-create path against MongoDB — no regression."""
    api_module.db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])
    api_module.db_manager.mongodb_adapter.write = AsyncMock(return_value=1)

    response = client.put(
        "/api/v1/mongodb/daily_pnl",
        json={
            "filter": {"symbol": "ETHUSDT"},
            "data": {"symbol": "ETHUSDT", "pnl": 5.0},
            "upsert": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated_count"] == 1
    api_module.db_manager.mongodb_adapter.write.assert_called_once()


def test_update_circuit_breaker_open_returns_503(client):
    """A tripped MySQL circuit breaker on update() surfaces as 503, not 500."""
    api_module.db_manager.mysql_adapter.query_range = Mock(
        return_value=[{"symbol": "BTCUSDT"}]
    )
    api_module.db_manager.mysql_adapter.update = Mock(
        side_effect=CircuitBreakerOpenError("mysql_write", 30)
    )

    response = client.put(
        "/api/v1/mysql/positions",
        json={"filter": {"symbol": "BTCUSDT"}, "data": {"quantity": 1.0}},
    )

    assert response.status_code == 503
