"""
Tests for volume calculator.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from data_manager.analytics.volume import VolumeCalculator
from data_manager.db.repositories import CandleRepository


@pytest.fixture
def volume_calculator(mock_db_manager):
    """Create volume calculator instance."""
    return VolumeCalculator(mock_db_manager)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volume_success(volume_calculator, mock_mongodb_adapter):
    """Test successful volume calculation."""
    # Create sample candle data
    candles = []
    base_time = datetime.utcnow() - timedelta(hours=24)

    for i in range(24):
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal("50000.0"),
            "high": Decimal("51000.0"),
            "low": Decimal("49000.0"),
            "close": Decimal("50500.0"),
            "volume": Decimal(str(1000.0 + i * 10))
        })

    # Mock repository
    volume_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    volume_calculator.db_manager.mongodb_adapter.write = AsyncMock()

    result = await volume_calculator.calculate_volume("BTCUSDT", "1h", 24)

    assert result is not None
    assert result.symbol == "BTCUSDT"
    assert result.timeframe == "1h"
    assert result.total_volume is not None
    assert result.volume_sma is not None
    assert result.volume_ema is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volume_insufficient_data(volume_calculator):
    """Test volume calculation with insufficient data."""
    # Mock repository to return insufficient data
    volume_calculator.candle_repo.get_range = AsyncMock(return_value=[])

    result = await volume_calculator.calculate_volume("BTCUSDT", "1h", 24)

    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volume_minimum_data(volume_calculator):
    """Test volume calculation with minimum required data."""
    candles = []
    base_time = datetime.utcnow() - timedelta(hours=5)

    # Create exactly 5 candles (minimum required)
    for i in range(5):
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal("50000.0"),
            "high": Decimal("51000.0"),
            "low": Decimal("49000.0"),
            "close": Decimal("50500.0"),
            "volume": Decimal("1000.0")
        })

    volume_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    volume_calculator.db_manager.mongodb_adapter.write = AsyncMock()

    result = await volume_calculator.calculate_volume("BTCUSDT", "1h", 24)

    assert result is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volume_error_handling(volume_calculator):
    """Test volume calculation error handling."""
    # Mock repository to raise exception
    volume_calculator.candle_repo.get_range = AsyncMock(side_effect=Exception("Database error"))

    result = await volume_calculator.calculate_volume("BTCUSDT", "1h", 24)

    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volume_stores_metrics(volume_calculator, mock_mongodb_adapter):
    """Test that volume metrics are stored in MongoDB."""
    candles = []
    base_time = datetime.utcnow() - timedelta(hours=24)

    for i in range(24):
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal("50000.0"),
            "high": Decimal("51000.0"),
            "low": Decimal("49000.0"),
            "close": Decimal("50500.0"),
            "volume": Decimal("1000.0")
        })

    volume_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    mock_mongodb_adapter.write = AsyncMock()

    await volume_calculator.calculate_volume("BTCUSDT", "1h", 24)

    # Verify write was called
    assert mock_mongodb_adapter.write.called


@pytest.mark.unit
def test_volume_calculator_initialization(mock_db_manager):
    """Test volume calculator initialization."""
    calculator = VolumeCalculator(mock_db_manager)

    assert calculator.db_manager == mock_db_manager
    assert calculator.candle_repo is not None
    assert isinstance(calculator.candle_repo, CandleRepository)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculate_volume_spike_ratio(volume_calculator):
    """Test volume spike ratio calculation."""
    candles = []
    base_time = datetime.utcnow() - timedelta(hours=24)

    # Create candles with a spike at the end
    for i in range(24):
        volume = Decimal("1000.0") if i < 23 else Decimal("5000.0")  # Spike at end
        candles.append({
            "symbol": "BTCUSDT",
            "timestamp": base_time + timedelta(hours=i),
            "open": Decimal("50000.0"),
            "high": Decimal("51000.0"),
            "low": Decimal("49000.0"),
            "close": Decimal("50500.0"),
            "volume": volume
        })

    volume_calculator.candle_repo.get_range = AsyncMock(return_value=candles)
    volume_calculator.db_manager.mongodb_adapter.write = AsyncMock()

    result = await volume_calculator.calculate_volume("BTCUSDT", "1h", 24)

    assert result is not None
    assert result.volume_spike_ratio > Decimal("1.0")  # Should detect spike

