"""
Pytest configuration and fixtures.
"""

import pytest


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
