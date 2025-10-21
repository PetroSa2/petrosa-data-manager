"""
Tests for API endpoints.
"""

import os

import pytest
from fastapi.testclient import TestClient

from data_manager.api.app import create_app


@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    return TestClient(app)


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


@pytest.mark.skipif(
    os.getenv("CI") == "true",
    reason="Requires database connectivity - skipped in CI",
)
def test_candles_endpoint(client):
    """Test candles data endpoint."""
    response = client.get("/data/candles?pair=BTCUSDT&period=1h")
    assert response.status_code == 200
    data = response.json()
    assert data["pair"] == "BTCUSDT"
    assert data["period"] == "1h"
    assert "values" in data
    assert "metadata" in data


@pytest.mark.skipif(
    os.getenv("CI") == "true",
    reason="Requires database connectivity - skipped in CI",
)
def test_volatility_endpoint(client):
    """Test volatility analytics endpoint."""
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
