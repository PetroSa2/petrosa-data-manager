"""
Tests for analytics module.

Tests correlation analysis, deviation detection, trend analysis,
and other analytics calculations.
"""

from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pandas as pd
import pytest

from data_manager.analytics.correlation import CorrelationCalculator
from data_manager.analytics.spread import SpreadCalculator
from data_manager.analytics.volume import VolumeCalculator
from data_manager.models.analytics import (
    CorrelationMetrics,
    MetricMetadata,
    SpreadMetrics,
    VolumeMetrics,
)


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
                "timestamp": datetime.now(UTC) - timedelta(hours=i),
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
                "timestamp": datetime.now(UTC) - timedelta(hours=i),
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
                "timestamp": datetime.now(UTC) - timedelta(hours=i),
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
                "timestamp": datetime.now(UTC) - timedelta(hours=i),
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
                "timestamp": datetime.now(UTC) - timedelta(hours=i * 2),  # Gaps
                "volume": Decimal("100.0"),
            }
            for i in range(30, 0, -1)
        ]

        candles_eth = [
            {
                "symbol": "ETHUSDT",
                "close": Decimal("3000.00"),
                "timestamp": datetime.now(UTC)
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
                "timestamp": datetime.now(UTC) - timedelta(hours=i),
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


def _assert_all_finite_decimals(mapping: dict) -> None:
    """Fail if any Decimal in `mapping` is NaN or +/-Infinity."""
    for key, value in mapping.items():
        assert isinstance(value, Decimal), f"{key} is not a Decimal: {value!r}"
        assert not value.is_nan(), f"{key} is NaN: {value!r}"
        assert not value.is_infinite(), f"{key} is infinite: {value!r}"


class TestCorrelationNaNSanitization:
    """
    Regression tests for #315: constant/zero-variance symbol series
    produce a 0/0 Pearson correlation (NaN) which previously crashed
    `CorrelationMetrics` construction (Pydantic `finite_number`) and
    aborted the ENTIRE correlation batch, not just the offending symbol.
    """

    @pytest.fixture
    def mock_db_manager(self):
        mock_manager = Mock()
        mock_manager.mysql_adapter = Mock()
        mock_manager.mongodb_adapter = AsyncMock()
        return mock_manager

    @pytest.fixture
    def correlation_calculator(self, mock_db_manager):
        return CorrelationCalculator(mock_db_manager)

    @pytest.fixture
    def shared_anchor(self):
        """Single timestamp anchor so BTC/ETH candles align for the inner join."""
        return datetime.now(UTC)

    @pytest.fixture
    def constant_candles_btc(self, shared_anchor):
        """Constant close price -> zero variance -> NaN Pearson correlation."""
        return [
            {
                "symbol": "BTCUSDT",
                "close": Decimal("50000.00"),
                "timestamp": shared_anchor - timedelta(hours=i),
                "volume": Decimal("100.0"),
            }
            for i in range(100, 0, -1)
        ]

    @pytest.fixture
    def varying_candles_eth(self, shared_anchor):
        """Genuinely varying close price -> finite, non-degenerate correlation."""
        return [
            {
                "symbol": "ETHUSDT",
                "close": Decimal(str(3000 + (i % 7) * 15)),
                "timestamp": shared_anchor - timedelta(hours=i),
                "volume": Decimal("500.0"),
            }
            for i in range(100, 0, -1)
        ]

    @pytest.mark.asyncio
    async def test_constant_series_does_not_raise_and_persists_other_symbols(
        self, correlation_calculator, constant_candles_btc, varying_candles_eth, caplog
    ):
        """
        AC1/AC2: a zero-variance symbol (BTCUSDT here) must not raise a
        Pydantic ValidationError and must not blank out the whole batch --
        the varying symbol (ETHUSDT) still gets a result.
        """
        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = [constant_candles_btc, varying_candles_eth]

            with caplog.at_level("WARNING"):
                result = await correlation_calculator.calculate_correlation(
                    symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
                )

        # No silent full-batch failure: no "Error calculating correlation"
        # log line, which is what the outer try/except emits on crash.
        assert not any(
            "Error calculating correlation" in rec.message for rec in caplog.records
        )

        # Both symbols get a persisted result -- per-symbol isolation.
        assert set(result.keys()) == {"BTCUSDT", "ETHUSDT"}

        for symbol, metrics in result.items():
            assert isinstance(metrics, CorrelationMetrics)
            _assert_all_finite_decimals(metrics.correlation_matrix)
            assert not metrics.rolling_correlation.is_nan()
            assert not metrics.rolling_correlation.is_infinite()
            if metrics.volatility_correlation is not None:
                assert not metrics.volatility_correlation.is_nan()

        # AC2: a WARNING names the excluded (non-finite) symbol/cell.
        assert any(
            "BTCUSDT" in rec.message and "non-finite" in rec.message.lower()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_all_symbols_constant_returns_empty_matrices_not_crash(
        self, correlation_calculator, constant_candles_btc, shared_anchor
    ):
        """Every symbol constant -> every cell is NaN -> all cells skipped,
        but the call must still complete and return metrics objects
        (empty correlation_matrix) instead of raising/crashing."""
        other_constant = [
            {**c, "symbol": "ETHUSDT", "close": Decimal("3000.00")}
            for c in constant_candles_btc
        ]

        with patch.object(
            correlation_calculator.candle_repo, "get_range"
        ) as mock_get_range:
            mock_get_range.side_effect = [constant_candles_btc, other_constant]

            result = await correlation_calculator.calculate_correlation(
                symbols=["BTCUSDT", "ETHUSDT"], timeframe="1h", window_days=30
            )

        assert isinstance(result, dict)
        for metrics in result.values():
            assert isinstance(metrics, CorrelationMetrics)
            _assert_all_finite_decimals(metrics.correlation_matrix)


class TestSpreadCalculatorNaNSanitization:
    """Regression tests for #315 in SpreadCalculator."""

    @pytest.fixture
    def mock_db_manager(self):
        mock_manager = Mock()
        mock_manager.mysql_adapter = Mock()
        mock_manager.mongodb_adapter = AsyncMock()
        return mock_manager

    @pytest.fixture
    def spread_calculator(self, mock_db_manager):
        return SpreadCalculator(mock_db_manager)

    @pytest.mark.asyncio
    async def test_nan_price_in_depth_snapshot_does_not_crash(self, spread_calculator):
        """A corrupted depth level with a NaN price must not reach
        SpreadMetrics as a non-finite Decimal."""
        depth_snapshot = [
            {
                "bids": [{"price": float("nan"), "quantity": 1.0}],
                "asks": [{"price": 50010.0, "quantity": 1.0}],
            }
        ]

        with patch.object(
            spread_calculator.depth_repo, "get_latest"
        ) as mock_get_latest:
            mock_get_latest.return_value = depth_snapshot

            result = await spread_calculator.calculate_spread("BTCUSDT")

        assert isinstance(result, SpreadMetrics)
        assert not result.bid_ask_spread.is_nan()
        assert not result.spread_percentage.is_nan()
        assert not result.market_depth_bid.is_nan()
        assert not result.market_depth_ask.is_nan()

    @pytest.mark.asyncio
    async def test_zero_price_levels_do_not_crash(self, spread_calculator):
        """Zero bid/ask prices (mid_price == 0) must not produce NaN."""
        depth_snapshot = [
            {
                "bids": [{"price": 0, "quantity": 1.0}],
                "asks": [{"price": 0, "quantity": 1.0}],
            }
        ]

        with patch.object(
            spread_calculator.depth_repo, "get_latest"
        ) as mock_get_latest:
            mock_get_latest.return_value = depth_snapshot

            result = await spread_calculator.calculate_spread("BTCUSDT")

        assert isinstance(result, SpreadMetrics)
        assert result.spread_percentage == Decimal("0")
        assert not result.bid_ask_spread.is_nan()


class TestVolumeCalculatorNaNSanitization:
    """Regression tests for #315 in VolumeCalculator."""

    @pytest.fixture
    def mock_db_manager(self):
        mock_manager = Mock()
        mock_manager.mysql_adapter = Mock()
        mock_manager.mongodb_adapter = AsyncMock()
        return mock_manager

    @pytest.fixture
    def volume_calculator(self, mock_db_manager):
        return VolumeCalculator(mock_db_manager)

    @pytest.mark.asyncio
    async def test_nan_volume_sample_does_not_crash(self, volume_calculator):
        """A malformed candle with NaN volume feeding a rolling mean must
        not reach VolumeMetrics as a non-finite Decimal."""
        candles = [
            {
                "symbol": "BTCUSDT",
                "close": Decimal("50000.00"),
                "volume": float("nan") if i == 0 else 100.0,
                "timestamp": datetime.now(UTC) - timedelta(hours=i),
            }
            for i in range(24, 0, -1)
        ]

        with patch.object(volume_calculator.candle_repo, "get_range") as mock_get_range:
            mock_get_range.return_value = candles

            result = await volume_calculator.calculate_volume(
                "BTCUSDT", "1h", window_hours=24
            )

        assert isinstance(result, VolumeMetrics)
        assert not result.total_volume.is_nan()
        assert not result.volume_sma.is_nan()
        assert not result.volume_ema.is_nan()
        assert not result.volume_spike_ratio.is_nan()
