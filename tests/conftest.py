import os
import pytest

# Disable OpenTelemetry auto-initialization during tests
os.environ['OTEL_NO_AUTO_INIT'] = '1'
os.environ['OTEL_SDK_DISABLED'] = 'true'
os.environ['OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED'] = 'false'

def pytest_configure(config):
    """
    Setup before any tests are run.
    """
    os.environ['OTEL_NO_AUTO_INIT'] = '1'
    os.environ['OTEL_SDK_DISABLED'] = 'true'

"""
Pytest configuration and fixtures for the Data Manager service.
"""

import os
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

# Disable OpenTelemetry auto-initialization during tests
os.environ["OTEL_NO_AUTO_INIT"] = "1"
os.environ["OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED"] = "false"
os.environ["ENVIRONMENT"] = "testing"


def pytest_configure(config):
    """Setup before any tests are run."""
    os.environ["OTEL_NO_AUTO_INIT"] = "1"


@pytest.fixture
def mock_nats_client():
    """Create a mock NATS client."""
    client = AsyncMock()
    client.is_connected = True
    client.connect = AsyncMock()
    client.subscribe = AsyncMock()
    client.publish = AsyncMock()
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_mongodb_client():
    """Create a mock MongoDB client."""
    client = Mock()
    db = Mock()
    collection = Mock()

    # Setup standard return values
    collection.find_one.return_value = None
    collection.insert_one.return_value = Mock(inserted_id="test_id")
    collection.find.return_value = []
    collection.count_documents.return_value = 0

    # Mock the dictionary-style access
    db.__getitem__.return_value = collection
    client.__getitem__.return_value = db

    return client


@pytest.fixture
def mock_mysql_connection():
    """Create a mock MySQL connection."""
    connection = Mock()
    cursor = Mock()

    cursor.execute = Mock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None

    # Support context manager
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection


@pytest.fixture
def test_streams() -> list[str]:
    """Test stream list."""
    return ["btcusdt@trade", "ethusdt@ticker"]


@pytest.fixture
def sample_metrics_data():
    """Sample business metrics data."""
    return {
        "service": "ta-bot",
        "symbol": "BTCUSDT",
        "strategy": "momentum_breakout",
        "metrics": {
            "win_rate": 0.65,
            "profit_factor": 1.8,
            "avg_trade_duration": 3600,
            "volatility_of_volatility": 0.01,
            "metadata": {
                "computed_at": datetime(2024, 1, 1, 0, 0, 0),
            },
        },
    }


@pytest.fixture
def sample_klines_data():
    """Sample klines data for database tests."""
    return [
        {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2024, 1, 1, 0, 0, 0),
            "open": 50000.0,
            "high": 51000.0,
            "low": 49000.0,
            "close": 50500.0,
            "volume": 100.0,
        },
        {
            "symbol": "BTCUSDT",
            "timestamp": datetime(2024, 1, 1, 1, 0, 0),
            "open": 50500.0,
            "high": 52000.0,
            "low": 50000.0,
            "close": 51500.0,
            "volume": 150.0,
        },
    ]
