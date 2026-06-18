from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from data_manager.api.routes.analysis import get_deviation, get_regime, get_seasonality


@pytest.mark.asyncio
async def test_get_regime_no_data_returns_200_with_null_data():
    """When no regime is computed, endpoint returns 200 with data=None (not 404).

    A 404 means 'route not found'. Missing computed data is a valid empty-state,
    not a routing error. CIO's from_api_response handles data=None gracefully.
    """
    mock_db = MagicMock()
    mock_adapter = AsyncMock()
    mock_db.mongodb_adapter = mock_adapter
    mock_adapter.query_latest.return_value = []

    with patch("data_manager.api.routes.analysis.api_module") as mock_api:
        mock_api.db_manager = mock_db

        result = await get_regime(pair="UNKNOWNPAIR", period="1h")

    assert isinstance(result, dict)
    assert result["pair"] == "UNKNOWNPAIR"
    assert result["metric"] == "regime"
    assert result["data"] is None
    assert "collection" in result["metadata"]


@pytest.mark.asyncio
async def test_get_regime_with_data_returns_200():
    """When regime data exists, endpoint returns 200 with populated data block."""
    mock_db = MagicMock()
    mock_adapter = AsyncMock()
    mock_db.mongodb_adapter = mock_adapter
    mock_adapter.query_latest.return_value = [
        {
            "regime": "CONSOLIDATION",
            "volatility_level": "low",
            "volume_level": "low",
            "trend_direction": "neutral",
            "confidence": "0.85",
            "metadata": {"computed_at": datetime(2026, 6, 18, 12, 0, 0)},
        }
    ]

    with patch("data_manager.api.routes.analysis.api_module") as mock_api:
        mock_api.db_manager = mock_db

        result = await get_regime(pair="BTCUSDT", period="1h")

    assert result["pair"] == "BTCUSDT"
    assert result["data"]["regime"] == "CONSOLIDATION"
    assert result["data"]["confidence"] == "0.85"


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
