"""
Tests for configuration rollback proxy endpoints.
"""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from data_manager.api.app import create_app
import data_manager.api.routes.config as config_routes

@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    # Mock db_manager to avoid initialization issues
    config_routes.db_manager = MagicMock()
    with TestClient(app) as client:
        yield client
    config_routes.db_manager = None

@pytest.fixture
def rollback_request():
    """Sample rollback request."""
    return {
        "target_version": 5,
        "changed_by": "test_user",
        "reason": "testing proxy"
    }

@pytest.mark.asyncio
async def test_proxy_rollback_ta_bot_app(client, rollback_request):
    """Test proxying app rollback to ta-bot."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True, "message": "Rolled back"}
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        response = client.post("/api/v1/config/ta-bot/rollback", json=rollback_request)
        
        assert response.status_code == 200
        assert response.json()["success"] is True
        # Verify correct URL was called
        args, kwargs = mock_post.call_args
        assert "ta-bot-service" in args[0]
        assert "application/rollback" in args[0]

@pytest.mark.asyncio
async def test_proxy_rollback_realtime_strategies(client, rollback_request):
    """Test proxying strategy rollback to realtime-strategies."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        response = client.post(
            "/api/v1/config/realtime-strategies/rollback?strategy_id=rsi&symbol=BTCUSDT", 
            json=rollback_request
        )
        
        assert response.status_code == 200
        # Verify correct URL and params
        args, kwargs = mock_post.call_args
        assert "realtime-strategies" in args[0]
        assert "strategies/rsi/rollback" in args[0]
        assert kwargs["params"]["symbol"] == "BTCUSDT"

def test_proxy_rollback_unknown_service(client, rollback_request):
    """Test proxying to an unknown service."""
    response = client.post("/api/v1/config/unknown-service/rollback", json=rollback_request)
    assert response.status_code == 400
    assert "Unknown service" in response.json()["detail"]

def test_proxy_rollback_missing_strategy_id(client, rollback_request):
    """Test missing strategy_id for realtime-strategies."""
    response = client.post("/api/v1/config/realtime-strategies/rollback", json=rollback_request)
    assert response.status_code == 400
    assert "strategy_id is required" in response.json()["detail"]

@pytest.mark.asyncio
async def test_proxy_history_tradeengine(client):
    """Test proxying history request to tradeengine."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": []}
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        response = client.get("/api/v1/config/tradeengine/history?limit=10")
        
        assert response.status_code == 200
        # Verify URL
        args, kwargs = mock_get.call_args
        assert "tradeengine-service" in args[0]
        assert "config/history" in args[0]
        assert kwargs["params"]["limit"] == 10

@pytest.mark.asyncio
async def test_proxy_timeout_handling(client, rollback_request):
    """Test timeout handling."""
    with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Timeout")):
        response = client.post("/api/v1/config/ta-bot/rollback", json=rollback_request)
        assert response.status_code == 504
        assert "Timeout connecting" in response.json()["detail"]

@pytest.mark.asyncio
async def test_proxy_downstream_error_propagation(client, rollback_request):
    """Test propagation of downstream errors."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Permission denied"
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        response = client.post("/api/v1/config/ta-bot/rollback", json=rollback_request)
        
        assert response.status_code == 403
        assert "ta-bot returned error" in response.json()["detail"]
        assert "Permission denied" in response.json()["detail"]
