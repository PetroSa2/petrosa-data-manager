"""
Tests for volatility calculator.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.analytics.volatility import VolatilityCalculator
from data_manager.db.repositories import CandleRepository


@pytest.fixture
def mock_candle_repo():
    """Mock candle repository."""
    mock_repo = Mock(spec=CandleRepository)
    return mock_repo


@pytest.fixture
def volatility_calculator(mock_db_manager):
    """Create volatility calculator instance."""
    return VolatilityCalculator(mock_db_manager)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volatility_success(volatility_calculator, mock_mongodb_adapter):
    """Test successful volatility calculation."""
    # Create sample candle data
    candles = []
    base_time = datetime.utcnow() - timedelta(days=30)
    base_price = 50000.0

    for i in range(30):
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal(str(base_price + i * 10)),
            "high": Decimal(str(base_price + i * 10 + 50)),
            "low": Decimal(str(base_price + i * 10 - 50)),
            "close": Decimal(str(base_price + i * 10 + 20)),
            "volume": Decimal("1000.0")
        })

    # Mock repository
    volatility_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    volatility_calculator.db_manager.mongodb_adapter.write = AsyncMock()

    result = await volatility_calculator.calculate_volatility("BTCUSDT", "1h", 30)

    assert result is not None
    assert result.symbol == "BTCUSDT"
    assert result.timeframe == "1h"
    assert result.rolling_stddev is not None
    assert result.annualized_volatility is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volatility_insufficient_data(volatility_calculator):
    """Test volatility calculation with insufficient data."""
    # Mock repository to return insufficient data
    volatility_calculator.candle_repo.get_range = AsyncMock(return_value=[])

    result = await volatility_calculator.calculate_volatility("BTCUSDT", "1h", 30)

    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volatility_minimum_data(volatility_calculator):
    """Test volatility calculation with minimum required data."""
    candles = []
    base_time = datetime.utcnow() - timedelta(days=1)

    # Create exactly 20 candles (minimum required)
    for i in range(20):
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal("50000.0"),
            "high": Decimal("51000.0"),
            "low": Decimal("49000.0"),
            "close": Decimal("50500.0"),
            "volume": Decimal("1000.0")
        })

    volatility_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    volatility_calculator.db_manager.mongodb_adapter.write = AsyncMock()

    result = await volatility_calculator.calculate_volatility("BTCUSDT", "1h", 30)

    assert result is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volatility_error_handling(volatility_calculator):
    """Test volatility calculation error handling."""
    # Mock repository to raise exception
    volatility_calculator.candle_repo.get_range = AsyncMock(side_effect=Exception("Database error"))

    result = await volatility_calculator.calculate_volatility("BTCUSDT", "1h", 30)

    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volatility_stores_metrics(volatility_calculator, mock_mongodb_adapter):
    """Test that volatility metrics are stored in MongoDB."""
    candles = []
    base_time = datetime.utcnow() - timedelta(days=30)

    for i in range(30):
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal("50000.0"),
            "high": Decimal("51000.0"),
            "low": Decimal("49000.0"),
            "close": Decimal("50500.0"),
            "volume": Decimal("1000.0")
        })

    volatility_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    mock_mongodb_adapter.write = AsyncMock()

    await volatility_calculator.calculate_volatility("BTCUSDT", "1h", 30)

    # Verify write was called with metrics and collection name
    assert mock_mongodb_adapter.write.called
    call_args = mock_mongodb_adapter.write.call_args
    # write is called as write([metrics], collection) - check first arg is list and second is collection name
    assert len(call_args[0]) >= 2
    assert isinstance(call_args[0][0], list)  # First arg is list of metrics
    assert call_args[0][1] == "analytics_BTCUSDT_volatility"  # Second arg is collection name


@pytest.mark.unit
def test_volatility_calculator_initialization(mock_db_manager):
    """Test volatility calculator initialization."""
    calculator = VolatilityCalculator(mock_db_manager)

    assert calculator.db_manager == mock_db_manager
    assert calculator.candle_repo is not None
    assert isinstance(calculator.candle_repo, CandleRepository)

