"""
Tests for API endpoints.
"""

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module


@pytest.fixture
def client(mock_db_manager):
    """Create test client with mocked database."""
    app = api_module.create_app()
    # Inject mock database manager into API module
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    # Cleanup
    api_module.db_manager = None


def test_root_endpoint(client):
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "petrosa-data-manager"
    assert "version" in data


def test_liveness_endpoint(client):
    """Test liveness probe endpoint."""
    response = client.get("/health/liveness")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_readiness_endpoint(client):
    """Test readiness probe endpoint."""
    response = client.get("/health/readiness")
    assert response.status_code == 200
    data = response.json()
    assert "ready" in data
    assert "components" in data


def test_candles_endpoint(client):
    """Test candles data endpoint with mocked database."""
    # The mongodb_adapter.query_range is already mocked in conftest.py
    # to return candle data, so this test should work directly

    response = client.get("/data/candles?pair=BTCUSDT&period=1h")
    assert response.status_code == 200
    data = response.json()
    assert data["pair"] == "BTCUSDT"
    assert data["period"] == "1h"
    assert "data" in data  # Endpoint returns "data" not "values"
    assert "metadata" in data


def test_volatility_endpoint(client):
    """Test volatility analytics endpoint with mocked database."""
    # The mongodb_adapter.query_latest is already mocked in conftest.py
    # to return volatility data, so this test should work directly

    response = client.get(
        "/analysis/volatility?pair=BTCUSDT&period=1h&method=rolling_stddev&window=30d"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["pair"] == "BTCUSDT"
    assert data["metric"] == "volatility"


def test_catalog_datasets_endpoint(client):
    """Test catalog datasets list endpoint."""
    response = client.get("/catalog/datasets")
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "pagination" in data
    assert "total" in data["pagination"]
    assert "limit" in data["pagination"]
    assert "offset" in data["pagination"]
