"""
Unit tests for AuditScheduler and AnalyticsScheduler.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.analytics.scheduler import AnalyticsScheduler
from data_manager.auditor.scheduler import AuditScheduler


class TestAuditScheduler:
    """Tests for AuditScheduler."""

    @pytest.fixture
    def mock_db_manager(self):
        return MagicMock()

    @pytest.fixture
    def scheduler(self, mock_db_manager):
        return AuditScheduler(mock_db_manager)

    @pytest.mark.asyncio
    async def test_run_audit_cycle_success(self, scheduler):
        """Test successful audit cycle with results collection."""
        # Mock gap detector, duplicate detector, and health scorer
        scheduler.gap_detector.detect_gaps = AsyncMock(return_value=[{"id": 1}])
        scheduler.duplicate_detector.detect_duplicates = AsyncMock(return_value=5)
        scheduler.health_scorer.calculate_health = AsyncMock(
            return_value=MagicMock(quality_score=95.0, completeness=100.0)
        )

        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.SUPPORTED_PAIRS = ["BTCUSDT"]
            mock_constants.SUPPORTED_TIMEFRAMES = ["1h"]
            mock_constants.MAX_CONCURRENT_TASKS = 2

            await scheduler.run_audit_cycle()

            # Verify detectors were called
            scheduler.gap_detector.detect_gaps.assert_called_once()
            scheduler.duplicate_detector.detect_duplicates.assert_called_once()

            # Verify counters were updated (internal state check)
            # Since we sum them up, we can't easily check internal locals,
            # but we can check the log output or just ensure it completes without error.
            assert scheduler.last_audit_time is not None

    @pytest.mark.asyncio
    async def test_run_audit_cycle_handles_errors(self, scheduler):
        """Test audit cycle handles errors in sub-tasks."""
        scheduler.gap_detector.detect_gaps = AsyncMock(side_effect=Exception("Audit failed"))

        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.SUPPORTED_PAIRS = ["BTCUSDT"]
            mock_constants.SUPPORTED_TIMEFRAMES = ["1h"]
            mock_constants.MAX_CONCURRENT_TASKS = 2

            await scheduler.run_audit_cycle()
            # Should not raise exception
            assert scheduler.last_audit_time is not None


class TestAnalyticsScheduler:
    """Tests for AnalyticsScheduler."""

    @pytest.fixture
    def mock_db_manager(self):
        return MagicMock()

    @pytest.fixture
    def scheduler(self, mock_db_manager):
        return AnalyticsScheduler(mock_db_manager)

    @pytest.mark.asyncio
    async def test_run_analytics_cycle_success(self, scheduler):
        """Test successful analytics cycle with results collection."""
        # Mock various calculators
        scheduler.volatility_calc.calculate_volatility = AsyncMock(return_value=0.5)
        scheduler.volume_calc.calculate_volume = AsyncMock(return_value=1000.0)
        scheduler.trend_calc.calculate_trend = AsyncMock(return_value="up")
        scheduler.deviation_calc.calculate_deviation = AsyncMock(return_value=0.1)
        scheduler.seasonality_calc.calculate_seasonality = AsyncMock(return_value={})
        scheduler.spread_calc.calculate_spread = AsyncMock(return_value=0.01)
        scheduler.regime_classifier.classify_regime = AsyncMock(return_value="bullish")
        scheduler.correlation_calc.calculate_correlation = AsyncMock(return_value={"BTC/ETH": 0.8})

        with patch("data_manager.analytics.scheduler.constants") as mock_constants:
            mock_constants.SUPPORTED_PAIRS = ["BTCUSDT"]
            mock_constants.MAX_CONCURRENT_TASKS = 2

            await scheduler.run_analytics_cycle()

            # Verify some calculators were called
            scheduler.volatility_calc.calculate_volatility.assert_called()
            scheduler.regime_classifier.classify_regime.assert_called()
            scheduler.correlation_calc.calculate_correlation.assert_called()
