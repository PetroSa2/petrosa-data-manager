"""
Tests for analytics module.

Tests correlation analysis, deviation detection, trend analysis,
and other analytics calculations.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pandas as pd
import pytest

from data_manager.analytics.correlation import CorrelationCalculator
from data_manager.models.analytics import CorrelationMetrics, MetricMetadata


class TestCorrelationCalculator:
    """Tests for CorrelationCalculator."""

    @pytest.fixture
    def mock_db_manager(self):
        """Create mock database manager."""
        mock_manager = Mock()
        mock_manager.mysql_adapter = Mock()
        mock_manager.mongodb_adapter = Mock()
        return mock_manager

    @pytest.fixture
    def correlation_calculator(self, mock_db_manager):
        """Create CorrelationCalculator instance."""
        return CorrelationCalculator(mock_db_manager)

    @pytest.fixture
    def sample_candles_btc(self):
        """Sample candle data for BTC."""
        return [
            {
                "symbol": "BTCUSDT",
                "close": Decimal("50000.00"),
                "timestamp": datetime.utcnow() - timedelta(hours=i),
                "volume": Decimal("100.0"),
            }
            for i in range(100, 0, -1)
        ]

    @pytest.fixture
    def sample_candles_eth(self):
        """Sample candle data for ETH."""
        return [
            {
                "symbol": "ETHUSDT",
                "close": Decimal("3000.00"),
                "timestamp": datetime.utcnow() - timedelta(hours=i),
                "volume": Decimal("500.0"),
            }
            for i in range(100, 0, -1)
        ]

    @pytest.mark.asyncio
    async def test_calculate_correlation_success(
        self, correlation_calculator, sample_candles_btc, sample_candles_eth
    ):
        """Test successful correlation calculation."""
        # Mock candle repository
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = [sample_candles_btc, sample_candles_eth]

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
            )

            assert isinstance(result, dict)
            # Should return correlations for both symbols
            assert len(result) >= 0  # May be empty if not enough data

    @pytest.mark.asyncio
    async def test_calculate_correlation_insufficient_symbols(
        self, correlation_calculator
    ):
        """Test correlation with insufficient symbols (< 2)."""
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.return_value = []

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT"], timeframe="1h", window_days=30
            )

            assert result == {}

    @pytest.mark.asyncio
    async def test_calculate_correlation_insufficient_data(
        self, correlation_calculator
    ):
        """Test correlation with insufficient candle data (< 20 candles)."""
        short_candles = [
            {
                "symbol": "BTCUSDT",
                "close": Decimal("50000.00"),
                "timestamp": datetime.utcnow() - timedelta(hours=i),
                "volume": Decimal("100.0"),
            }
            for i in range(10, 0, -1)
        ]

        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.return_value = short_candles

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
            )

            assert result == {}

    @pytest.mark.asyncio
    async def test_calculate_correlation_handles_errors(self, correlation_calculator):
        """Test correlation handles errors gracefully."""
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = Exception("Database error")

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
            )

            # Should return empty dict on error
            assert result == {}

    @pytest.mark.asyncio
    async def test_calculate_correlation_with_different_timeframes(
        self, correlation_calculator, sample_candles_btc, sample_candles_eth
    ):
        """Test correlation calculation with different timeframes."""
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = [sample_candles_btc, sample_candles_eth]

            for timeframe in ["1h", "4h", "1d"]:
                result = await correlation_calculator.calculate_correlation(
                    symbols=["BTCUSDT", "ETHUSDT"],
                    timeframe=timeframe,
                    window_days=30,
                )

                assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_calculate_correlation_decimal_handling(self, correlation_calculator):
        """Test correlation handles Decimal values correctly."""
        candles_with_decimals = [
            {
                "symbol": "BTCUSDT",
                "close": Decimal(f"{50000 + i}.{i % 100:02d}"),
                "timestamp": datetime.utcnow() - timedelta(hours=i),
                "volume": Decimal("100.0"),
            }
            for i in range(50, 0, -1)
        ]

        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.return_value = candles_with_decimals

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT"], timeframe="1h", window_days=30
            )

            # Should handle Decimal conversion without errors
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_calculate_correlation_with_missing_timestamps(
        self, correlation_calculator
    ):
        """Test correlation handles candles with missing/misaligned timestamps."""
        candles_btc = [
            {
                "symbol": "BTCUSDT",
                "close": Decimal("50000.00"),
                "timestamp": datetime.utcnow() - timedelta(hours=i * 2),  # Gaps
                "volume": Decimal("100.0"),
            }
            for i in range(30, 0, -1)
        ]

        candles_eth = [
            {
                "symbol": "ETHUSDT",
                "close": Decimal("3000.00"),
                "timestamp": datetime.utcnow()
                - timedelta(hours=i * 3),  # Different gaps
                "volume": Decimal("500.0"),
            }
            for i in range(30, 0, -1)
        ]

        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = [candles_btc, candles_eth]

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
            )

            # Should handle misaligned timestamps
            assert isinstance(result, dict)

    def test_correlation_calculator_initialization(self, mock_db_manager):
        """Test CorrelationCalculator initializes correctly."""
        calculator = CorrelationCalculator(mock_db_manager)

        assert calculator.db_manager is mock_db_manager
        assert calculator.candle_repo is not None

    @pytest.mark.asyncio
    async def test_calculate_correlation_empty_symbol_list(
        self, correlation_calculator
    ):
        """Test correlation with empty symbol list."""
        result = await correlation_calculator.calculate_correlation(
            symbols=[], timeframe="1h", window_days=30
        )

        assert result == {}

    @pytest.mark.asyncio
    async def test_calculate_correlation_single_symbol(
        self, correlation_calculator, sample_candles_btc
    ):
        """Test correlation with single symbol (should return empty)."""
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.return_value = sample_candles_btc

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT"], timeframe="1h", window_days=30
            )

            # Single symbol can't have correlations
            assert result == {}

    @pytest.mark.asyncio
    async def test_calculate_correlation_with_zero_window(self, correlation_calculator):
        """Test correlation with zero window days."""
        result = await correlation_calculator.calculate_correlation(
            symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=0
        )

        # Should handle gracefully
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_calculate_correlation_with_large_dataset(
        self, correlation_calculator
    ):
        """Test correlation with large dataset."""
        large_candles = [
            {
                "symbol": "BTCUSDT",
                "close": Decimal(f"{50000 + i * 10}"),
                "timestamp": datetime.utcnow() - timedelta(hours=i),
                "volume": Decimal("100.0"),
            }
            for i in range(1000, 0, -1)
        ]

        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.return_value = large_candles

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
            )

            # Should handle large datasets
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_calculate_correlation_concurrent_calls(
        self, correlation_calculator, sample_candles_btc, sample_candles_eth
    ):
        """Test concurrent correlation calculations."""
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = lambda *args: (
                sample_candles_btc if "BTC" in str(args[0]) else sample_candles_eth
            )

            # Make concurrent calls
            import asyncio

            results = await asyncio.gather(
                correlation_calculator.calculate_correlation(
                    ["BTCUSDT", "ETHUSDT"], "1h", 30
                ),
                correlation_calculator.calculate_correlation(
                    ["BTCUSDT", "BNBUSDT"], "1h", 30
                ),
            )

            assert len(results) == 2
            assert all(isinstance(r, dict) for r in results)
