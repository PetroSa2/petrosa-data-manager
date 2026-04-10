import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock, AsyncMock, patch
from data_manager.api.routes.analysis import get_regime, get_deviation, get_seasonality

@pytest.mark.asyncio
async def test_get_regime_404_not_masked():
    """Verify that 404 is not masked as 500 in get_regime."""
    # Mock db_manager and mongodb_adapter
    mock_db = MagicMock()
    mock_adapter = AsyncMock()
    mock_db.mongodb_adapter = mock_adapter
    
    # query_latest returns empty list -> triggers 404
    mock_adapter.query_latest.return_value = []
    
    with patch("data_manager.api.routes.analysis.api_module") as mock_api:
        mock_api.db_manager = mock_db
        
        with pytest.raises(HTTPException) as exc_info:
            await get_regime(pair="UNKNOWN", period="1h")
        
        assert exc_info.value.status_code == 404
        assert "No regime data available" in exc_info.value.detail

@pytest.mark.asyncio
async def test_get_deviation_404_not_masked():
    """Verify that 404 is not masked as 500 in get_deviation."""
    mock_db = MagicMock()
    mock_adapter = AsyncMock()
    mock_db.mongodb_adapter = mock_adapter
    mock_adapter.query_latest.return_value = []
    
    with patch("data_manager.api.routes.analysis.api_module") as mock_api:
        mock_api.db_manager = mock_db
        
        with pytest.raises(HTTPException) as exc_info:
            await get_deviation(pair="UNKNOWN", period="1h")
        
        assert exc_info.value.status_code == 404
        assert "No deviation data available" in exc_info.value.detail

@pytest.mark.asyncio
async def test_get_seasonality_404_not_masked():
    """Verify that 404 is not masked as 500 in get_seasonality."""
    mock_db = MagicMock()
    mock_adapter = AsyncMock()
    mock_db.mongodb_adapter = mock_adapter
    mock_adapter.query_latest.return_value = []
    
    with patch("data_manager.api.routes.analysis.api_module") as mock_api:
        mock_api.db_manager = mock_db
        
        with pytest.raises(HTTPException) as exc_info:
            await get_seasonality(pair="UNKNOWN", period="1h")
        
        assert exc_info.value.status_code == 404
        assert "No seasonality data available" in exc_info.value.detail
