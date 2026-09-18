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
    def mock_nats_client(self):
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.connect = AsyncMock(return_value=True)
        client.subscribe = AsyncMock(return_value=MagicMock())
        return client

    @pytest.fixture
    def scheduler(self, mock_db_manager):
        return AuditScheduler(mock_db_manager)

    @pytest.fixture
    def scheduler_with_nats(self, mock_db_manager, mock_nats_client):
        return AuditScheduler(
            mock_db_manager,
            backfill_orchestrator=MagicMock(),
            nats_client=mock_nats_client,
        )

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
        scheduler.gap_detector.detect_gaps = AsyncMock(
            side_effect=Exception("Audit failed")
        )

        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.SUPPORTED_PAIRS = ["BTCUSDT"]
            mock_constants.SUPPORTED_TIMEFRAMES = ["1h"]
            mock_constants.MAX_CONCURRENT_TASKS = 2

            await scheduler.run_audit_cycle()
            # Should not raise exception
            assert scheduler.last_audit_time is not None


class TestAuditSchedulerStreaming:
    """Tests for AuditScheduler streaming gap detector integration."""

    @pytest.fixture
    def mock_db_manager(self):
        return MagicMock()

    @pytest.fixture
    def mock_nats_client(self):
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.connect = AsyncMock(return_value=True)
        client.subscribe = AsyncMock(return_value=MagicMock())
        return client

    @pytest.mark.asyncio
    async def test_streaming_detector_initialized_when_enabled(
        self, mock_db_manager, mock_nats_client
    ):
        """When ENABLE_STREAMING_GAP_DETECTION is true, the scheduler creates a streaming detector."""
        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.ENABLE_STREAMING_GAP_DETECTION = True
            scheduler = AuditScheduler(mock_db_manager, nats_client=mock_nats_client)
            assert scheduler.streaming_detector is not None

    @pytest.mark.asyncio
    async def test_streaming_detector_not_initialized_when_disabled(
        self, mock_db_manager
    ):
        """When ENABLE_STREAMING_GAP_DETECTION is false, no streaming detector is created."""
        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.ENABLE_STREAMING_GAP_DETECTION = False
            scheduler = AuditScheduler(mock_db_manager)
            assert scheduler.streaming_detector is None

    @pytest.mark.asyncio
    async def test_get_status_includes_streaming_info(
        self, mock_db_manager, mock_nats_client
    ):
        """get_status includes streaming detector info when available."""
        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.ENABLE_STREAMING_GAP_DETECTION = True
            scheduler = AuditScheduler(mock_db_manager, nats_client=mock_nats_client)
            status = scheduler.get_status()
            assert "streaming_detector" in status

    @pytest.mark.asyncio
    async def test_get_status_without_streaming(self, mock_db_manager):
        """get_status does not include streaming info when detector is None."""
        with patch("data_manager.auditor.scheduler.constants") as mock_constants:
            mock_constants.ENABLE_STREAMING_GAP_DETECTION = False
            scheduler = AuditScheduler(mock_db_manager)
            status = scheduler.get_status()
            assert "streaming_detector" not in status


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
        scheduler.correlation_calc.calculate_correlation = AsyncMock(
            return_value={"BTC/ETH": 0.8}
        )

        with patch("data_manager.analytics.scheduler.constants") as mock_constants:
            mock_constants.SUPPORTED_PAIRS = ["BTCUSDT"]
            mock_constants.MAX_CONCURRENT_TASKS = 2

            await scheduler.run_analytics_cycle()

            # Verify some calculators were called
            scheduler.volatility_calc.calculate_volatility.assert_called()
            scheduler.regime_classifier.classify_regime.assert_called()
            scheduler.correlation_calc.calculate_correlation.assert_called()
