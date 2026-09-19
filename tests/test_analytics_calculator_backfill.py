"""
Tests that each analytics calculator calls _insufficient_data
when it detects insufficient data (AC5: calculator with 2 candles → backfill triggered).
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Force the env-var before any imports
os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "false"

from data_manager.analytics.deviation import DeviationCalculator
from data_manager.analytics.seasonality import SeasonalityCalculator
from data_manager.analytics.trend import TrendCalculator
from data_manager.analytics.volatility import VolatilityCalculator
from data_manager.analytics.volume import VolumeCalculator


def _make_db() -> MagicMock:
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    return db


# Helper: candles with 2 entries (insufficient for all calculators)
def _two_candles():
    now = datetime.now(UTC)
    return [
        {
            "symbol": "BTCUSDT",
            "close": Decimal("50000.00"),
            "open": Decimal("49900.00"),
            "high": Decimal("50100.00"),
            "low": Decimal("49800.00"),
            "volume": Decimal("100.0"),
            "timestamp": now - timedelta(hours=2),
        },
        {
            "symbol": "BTCUSDT",
            "close": Decimal("50200.00"),
            "open": Decimal("50000.00"),
            "high": Decimal("50300.00"),
            "low": Decimal("49900.00"),
            "volume": Decimal("120.0"),
            "timestamp": now - timedelta(hours=1),
        },
    ]


def _enable_backfill():
    """Enable backfill via env-var."""
    os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "true"


def _disable_backfill():
    """Disable backfill via env-var."""
    os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "false"


class TestTrendCalculatorInsufficientData:
    @pytest.mark.asyncio
    async def test_returns_none_and_calls_insufficient_data(self):
        _enable_backfill()
        try:
            calc = TrendCalculator(_make_db())
            trigger = AsyncMock()
            trigger.on_verdict = AsyncMock(return_value=[])
            calc.backfill_trigger = trigger

            with patch.object(calc.candle_repo, "get_range") as mock_get:
                mock_get.return_value = _two_candles()
                result = await calc.calculate_trend("BTCUSDT", "1h", window_days=30)

            assert result is None
            trigger.on_verdict.assert_called_once()
        finally:
            _disable_backfill()


class TestVolumeCalculatorInsufficientData:
    @pytest.mark.asyncio
    async def test_returns_none_and_calls_insufficient_data(self):
        _enable_backfill()
        try:
            calc = VolumeCalculator(_make_db())
            trigger = AsyncMock()
            trigger.on_verdict = AsyncMock(return_value=[])
            calc.backfill_trigger = trigger

            with patch.object(calc.candle_repo, "get_range") as mock_get:
                mock_get.return_value = _two_candles()
                result = await calc.calculate_volume("BTCUSDT", "1h", window_hours=24)

            assert result is None
            trigger.on_verdict.assert_called_once()
        finally:
            _disable_backfill()


class TestSeasonalityCalculatorInsufficientData:
    @pytest.mark.asyncio
    async def test_returns_none_and_calls_insufficient_data(self):
        _enable_backfill()
        try:
            calc = SeasonalityCalculator(_make_db())
            trigger = AsyncMock()
            trigger.on_verdict = AsyncMock(return_value=[])
            calc.backfill_trigger = trigger

            with patch.object(calc.candle_repo, "get_range") as mock_get:
                mock_get.return_value = _two_candles()
                result = await calc.calculate_seasonality(
                    "BTCUSDT", "1h", window_days=90
                )

            assert result is None
            trigger.on_verdict.assert_called_once()
        finally:
            _disable_backfill()


class TestVolatilityCalculatorInsufficientData:
    @pytest.mark.asyncio
    async def test_returns_none_and_calls_insufficient_data(self):
        _enable_backfill()
        try:
            calc = VolatilityCalculator(_make_db())
            trigger = AsyncMock()
            trigger.on_verdict = AsyncMock(return_value=[])
            calc.backfill_trigger = trigger

            with patch.object(calc.candle_repo, "get_range") as mock_get:
                mock_get.return_value = _two_candles()
                result = await calc.calculate_volatility(
                    "BTCUSDT", "1h", window_days=30
                )

            assert result is None
            trigger.on_verdict.assert_called_once()
        finally:
            _disable_backfill()


class TestDeviationCalculatorInsufficientData:
    @pytest.mark.asyncio
    async def test_returns_none_and_calls_insufficient_data(self):
        _enable_backfill()
        try:
            calc = DeviationCalculator(_make_db())
            trigger = AsyncMock()
            trigger.on_verdict = AsyncMock(return_value=[])
            calc.backfill_trigger = trigger

            with patch.object(calc.candle_repo, "get_range") as mock_get:
                mock_get.return_value = _two_candles()
                result = await calc.calculate_deviation("BTCUSDT", "1h", window_days=30)

            assert result is None
            trigger.on_verdict.assert_called_once()
        finally:
            _disable_backfill()
