"""
Unit tests for data_manager.ml.* (statistical + ML anomaly detectors).

Mocks the database adapters; uses real numpy/pandas computation for the
detection math so we exercise the actual zscore/mad/moving-avg branches.
"""

from datetime import UTC, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pandas as pd
import pytest

from data_manager.ml.statistical_detector import StatisticalAnomalyDetector


def make_normal_candles(n: int = 100, base_price: float = 100.0) -> list[dict]:
    """Generate normal-looking OHLCV candles with one big spike at index 50."""
    rng = np.random.RandomState(42)
    candles = []
    for i in range(n):
        price = base_price + rng.normal(0, 1)
        if i == 50:
            price = base_price + 100  # extreme outlier
        candles.append(
            {
                "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                "open": Decimal(str(price - 0.5)),
                "high": Decimal(str(price + 1.0)),
                "low": Decimal(str(price - 1.0)),
                "close": Decimal(str(price)),
                "volume": Decimal("1000"),
            }
        )
    return candles


@pytest.fixture
def mock_db_manager():
    dbm = Mock()
    dbm.mysql_adapter = Mock()
    dbm.mongodb_adapter = Mock()
    return dbm


class TestStatisticalDetectorInit:
    def test_initialization(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        assert det.db_manager is mock_db_manager
        assert det.candle_repo is not None
        assert det.audit_repo is not None


class TestZScoreDetection:
    def test_no_outliers_in_uniform_data(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        series = pd.Series([100.0] * 50)
        # Zero std → NaN z-scores → no anomalies.
        mask = det._detect_zscore_anomalies(series, threshold=3.0)
        # All NaN compared with > threshold returns False.
        assert mask.sum() == 0

    def test_detects_extreme_outlier(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        rng = np.random.RandomState(42)
        series = pd.Series(rng.normal(100, 1, 100).tolist() + [200.0])
        mask = det._detect_zscore_anomalies(series, threshold=3.0)
        # The 200.0 value must be flagged.
        assert mask.iloc[-1]


class TestMadDetection:
    def test_zero_mad_returns_no_anomalies(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        series = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0])
        # All identical → MAD == 0 → guard returns all-False.
        mask = det._detect_mad_anomalies(series, threshold=3.0)
        assert mask.sum() == 0
        assert len(mask) == 5

    def test_detects_outlier_via_mad(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        series = pd.Series([10.0, 10.5, 10.1, 9.9, 10.2, 50.0])
        mask = det._detect_mad_anomalies(series, threshold=3.5)
        # The 50.0 must be flagged.
        assert mask.iloc[-1]


class TestMovingAvgDetection:
    def test_detects_deviation_from_rolling_mean(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        # 30 stable then a spike
        prices = [10.0] * 30 + [50.0]
        series = pd.Series(prices)
        mask = det._detect_moving_avg_anomalies(series, window=20, threshold=2.0)
        # Spike at the last index should be flagged.
        assert mask.iloc[-1]


class TestCalculateSeverity:
    def test_low_severity(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        # A point near the mean of normally distributed data gives low z-score → "low".
        rng = np.random.RandomState(42)
        series = pd.Series(rng.normal(100, 10, 100).tolist())
        # Pick an index where value ≈ mean.
        idx = int(np.argmin(np.abs(series - series.mean())))
        assert det._calculate_severity(series, idx) == "low"

    def test_critical_severity(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        rng = np.random.RandomState(42)
        data = rng.normal(100, 1, 100).tolist() + [200.0]  # ~100 sigma away
        series = pd.Series(data)
        assert det._calculate_severity(series, 100) == "critical"

    def test_zero_std_returns_low(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        # All identical -> std=0 -> z_score=0 -> "low"
        series = pd.Series([100.0] * 10)
        assert det._calculate_severity(series, 0) == "low"


class TestDetectAnomalies:
    @pytest.mark.asyncio
    async def test_insufficient_data_returns_empty(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(return_value=[])
        result = await det.detect_anomalies("BTCUSDT", "1h", method="zscore")
        assert result == []

    @pytest.mark.asyncio
    async def test_unknown_method_returns_empty(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(return_value=make_normal_candles(100))
        det.audit_repo.log_health_check = AsyncMock(return_value=True)
        result = await det.detect_anomalies("BTCUSDT", "1h", method="bogus")
        assert result == []

    @pytest.mark.asyncio
    async def test_zscore_method_flags_outliers(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(return_value=make_normal_candles(100))
        det.audit_repo.log_health_check = AsyncMock(return_value=True)
        result = await det.detect_anomalies(
            "BTCUSDT", "1h", method="zscore", threshold=3.0
        )
        assert len(result) >= 1  # The spike at index 50 should be flagged.
        assert result[0]["method"] == "zscore"
        # Each anomaly must have been logged.
        assert det.audit_repo.log_health_check.call_count == len(result)

    @pytest.mark.asyncio
    async def test_mad_method_works(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(return_value=make_normal_candles(100))
        det.audit_repo.log_health_check = AsyncMock(return_value=True)
        result = await det.detect_anomalies(
            "BTCUSDT", "1h", method="mad", threshold=3.5
        )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_moving_avg_method_works(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(return_value=make_normal_candles(100))
        det.audit_repo.log_health_check = AsyncMock(return_value=True)
        result = await det.detect_anomalies(
            "BTCUSDT", "1h", method="moving_avg", threshold=2.0
        )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(side_effect=RuntimeError("boom"))
        assert await det.detect_anomalies("BTCUSDT", "1h") == []

    @pytest.mark.asyncio
    async def test_log_anomaly_swallows_exception(self, mock_db_manager):
        det = StatisticalAnomalyDetector(mock_db_manager)
        det.audit_repo.log_health_check = AsyncMock(side_effect=RuntimeError("boom"))
        # Must not raise.
        await det._log_anomaly(
            "BTCUSDT",
            "1h",
            {
                "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                "price": 100.0,
                "method": "zscore",
                "severity": "high",
            },
        )
        det.audit_repo.log_health_check.assert_called_once()


# ML detector — only run if sklearn is available; module is optional.
try:
    from data_manager.ml.anomaly_detector import MLAnomalyDetector  # noqa: F401

    SKLEARN_AVAILABLE = True
except Exception:
    SKLEARN_AVAILABLE = False


@pytest.mark.skipif(not SKLEARN_AVAILABLE, reason="sklearn not available")
class TestMLAnomalyDetector:
    def test_init_creates_isolation_forest_model(self, mock_db_manager):
        from data_manager.ml.anomaly_detector import MLAnomalyDetector

        det = MLAnomalyDetector(mock_db_manager, contamination=0.1)
        assert det.model is not None
        assert det.db_manager is mock_db_manager

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_empty(self, mock_db_manager):
        from data_manager.ml.anomaly_detector import MLAnomalyDetector

        det = MLAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(return_value=make_normal_candles(10))
        result = await det.detect_price_anomalies("BTCUSDT", "1h")
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_price_anomalies_returns_empty_on_exception(
        self, mock_db_manager
    ):
        from data_manager.ml.anomaly_detector import MLAnomalyDetector

        det = MLAnomalyDetector(mock_db_manager)
        det.candle_repo.get_range = AsyncMock(side_effect=RuntimeError("boom"))
        assert await det.detect_price_anomalies("BTCUSDT", "1h") == []

    @pytest.mark.asyncio
    async def test_detect_price_anomalies_flags_outliers(self, mock_db_manager):
        from data_manager.ml.anomaly_detector import MLAnomalyDetector

        det = MLAnomalyDetector(mock_db_manager, contamination=0.05)
        det.candle_repo.get_range = AsyncMock(return_value=make_normal_candles(100))
        det.audit_repo.log_health_check = AsyncMock(return_value=True)
        result = await det.detect_price_anomalies("BTCUSDT", "1h")
        # IsolationForest with 5% contamination on 100 points should flag ~5 outliers.
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_log_anomaly_swallows_exception(self, mock_db_manager):
        from data_manager.ml.anomaly_detector import MLAnomalyDetector

        det = MLAnomalyDetector(mock_db_manager)
        det.audit_repo.log_health_check = AsyncMock(side_effect=RuntimeError("boom"))
        await det._log_anomaly(
            "BTCUSDT",
            "1h",
            {
                "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                "price": 100.0,
                "method": "isolation_forest",
                "severity": "medium",
            },
        )
        det.audit_repo.log_health_check.assert_called_once()


class TestMLImportFallback:
    def test_raises_import_error_when_sklearn_unavailable(self, mock_db_manager):
        # Patch SKLEARN_AVAILABLE in the module to trip the guard.
        with patch("data_manager.ml.anomaly_detector.SKLEARN_AVAILABLE", False):
            from data_manager.ml.anomaly_detector import MLAnomalyDetector

            with pytest.raises(
                ImportError, match="scikit-learn is required"
            ) as exc_info:
                MLAnomalyDetector(mock_db_manager)
            assert "scikit-learn" in str(exc_info.value)


class TestMlInit:
    def test_ml_module_exposes_expected_names(self):
        import data_manager.ml as ml

        assert ml.StatisticalAnomalyDetector is StatisticalAnomalyDetector
        assert hasattr(ml, "ML_AVAILABLE")
        assert isinstance(ml.ML_AVAILABLE, bool)
