"""
Tests for legacy backward-compatibility API endpoints.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked database."""
    # Ensure mongodb_adapter and mysql_adapter are mocks with AsyncMock methods
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.query_range = AsyncMock(return_value=[])
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)

    mock_db_manager.mysql_adapter = Mock()
    mock_db_manager.mysql_adapter.query_range = Mock(return_value=[])
    mock_db_manager.mysql_adapter.write = Mock(return_value=1)

    app = api_module.create_app()
    # Inject mock database manager into API module
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    # Cleanup
    api_module.db_manager = None


def test_legacy_query_endpoint(client):
    """Test legacy query endpoint."""
    payload = {
        "database": "mongodb",
        "collection": "candles_BTCUSDT_1h",
        "filter": {"symbol": "BTCUSDT"},
        "limit": 10
    }
    response = client.post("/api/v1/data/query", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "pagination" in data
    assert data["metadata"]["collection"] == "candles_BTCUSDT_1h"


def test_legacy_insert_endpoint(client):
    """Test legacy insert endpoint."""
    payload = {
        "database": "mongodb",
        "collection": "candles_BTCUSDT_1h",
        "records": [
            {"symbol": "BTCUSDT", "open": 50000, "close": 51000}
        ]
    }
    response = client.post("/api/v1/data/insert", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "inserted_count" in data
    assert data["metadata"]["collection"] == "candles_BTCUSDT_1h"


def test_legacy_query_missing_collection(client):
    """Test legacy query with missing collection name."""
    payload = {
        "database": "mongodb",
        "filter": {}
    }
    response = client.post("/api/v1/data/query", json=payload)
    assert response.status_code == 400
    assert "Collection name required" in response.json()["detail"]


def test_generic_get_records(client):
    """Test the generic get_records endpoint which uses the same internal logic."""
    response = client.get("/api/v1/mongodb/candles_BTCUSDT_1h")
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert data["metadata"]["collection"] == "candles_BTCUSDT_1h"
