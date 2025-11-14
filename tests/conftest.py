"""
Pytest configuration and fixtures.
"""

import os
from unittest.mock import AsyncMock, Mock

import pytest

# Set OTEL_NO_AUTO_INIT before any imports to prevent auto-initialization
os.environ["OTEL_NO_AUTO_INIT"] = "1"
os.environ["ENVIRONMENT"] = "testing"


@pytest.fixture
def mock_nats_client():
    """Mock NATS client for testing."""
    # TODO: Implement mock NATS client
    pass


@pytest.fixture
def sample_market_data_event():
    """Sample market data event for testing."""
    return {
        "e": "trade",
        "s": "BTCUSDT",
        "t": 12345,
        "p": "50000.00",
        "q": "0.1",
        "T": 1633046400000,
    }


@pytest.fixture
def sample_candle_data():
    """Sample candle data for testing."""
    return {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "open": "50000.00",
        "high": "51000.00",
        "low": "49500.00",
        "close": "50500.00",
        "volume": "1000.0",
    }


@pytest.fixture
def mock_mysql_adapter():
    """Mock MySQL adapter for testing."""
    mock_adapter = Mock()
    mock_adapter.query = AsyncMock(return_value=[])
    mock_adapter.execute = AsyncMock(return_value=True)
    return mock_adapter


@pytest.fixture
def mock_mongodb_adapter():
    """Mock MongoDB adapter for testing."""
    mock_adapter = Mock()
    
    # Mock query_latest to return sample volatility data
    mock_adapter.query_latest = AsyncMock(return_value=[
        {
            "symbol": "BTCUSDT",
            "metric": "volatility",
            "value": 0.05,
            "timestamp": "2024-01-01T00:00:00Z",
        }
    ])
    
    # Mock query_range to return sample candle data
    mock_adapter.query_range = AsyncMock(return_value=[
        {
            "symbol": "BTCUSDT",
            "timestamp": "2024-01-01T00:00:00Z",
            "open": 50000.0,
            "high": 51000.0,
            "low": 49500.0,
            "close": 50500.0,
            "volume": 1000.0,
        }
    ])
    
    # Mock other common methods
    mock_adapter.find = AsyncMock(return_value=[])
    mock_adapter.insert = AsyncMock(return_value=True)
    mock_adapter.update = AsyncMock(return_value=True)
    mock_adapter.write = AsyncMock(return_value=1)
    
    return mock_adapter


@pytest.fixture
def mock_db_manager(mock_mysql_adapter, mock_mongodb_adapter):
    """Mock DatabaseManager for API endpoint testing."""
    mock_manager = Mock()
    mock_manager.mysql_adapter = mock_mysql_adapter
    mock_manager.mongodb_adapter = mock_mongodb_adapter
    mock_manager.is_connected = True
    return mock_manager
