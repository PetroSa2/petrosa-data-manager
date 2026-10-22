"""Regression tests for petrosa-data-manager#330.

A duplicate timestamp inflates `candle_repo.count` above the expected
record count for the lookback window, which previously pushed
`completeness` (and the derived `quality_score`) above the
`DataHealthMetrics` `le=100.0` bound and raised a `ValidationError`.
`HealthScorer.calculate_health` swallowed that error and returned
all-zero fallback metrics, masking the true health of the dataset.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from data_manager.auditor.health_scorer import (
    HEALTH_METRICS_VALIDATION_CHECKS,
    HEALTH_METRICS_VALIDATION_ERRORS,
    HealthScorer,
)
from data_manager.models.health import DataHealthMetrics
from data_manager.observability.thin_data_evidence import THIN_DATA_CONSUMER_ERRORS


def _make_db_manager():
    manager = MagicMock()
    manager.mysql_adapter = MagicMock()
    manager.mongodb_adapter = MagicMock()
    return manager


class TestHealthScorerCompletenessClamp:
    @pytest.mark.asyncio
    async def test_validation_error_emits_dedicated_evidence(self, monkeypatch, caplog):
        db_manager = _make_db_manager()
        scorer = HealthScorer(db_manager)
        scorer.candle_repo.count = AsyncMock(return_value=1)
        scorer.candle_repo.get_latest = AsyncMock(
            return_value=[{"timestamp": datetime.now(UTC) - timedelta(seconds=30)}]
        )

        with pytest.raises(ValidationError) as error:
            DataHealthMetrics(
                completeness=101.0,
                freshness_seconds=1,
                gaps_count=0,
                duplicates_count=0,
                consistency_score=100.0,
                quality_score=100.0,
            )
        original_model = DataHealthMetrics
        calls = 0

        def fail_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error.value
            return original_model(**kwargs)

        monkeypatch.setattr(
            "data_manager.auditor.health_scorer.DataHealthMetrics", fail_once
        )

        before = HEALTH_METRICS_VALIDATION_ERRORS.labels(
            symbol="ADAUSDT", timeframe="1h"
        )._value.get()
        evidence_before = THIN_DATA_CONSUMER_ERRORS.labels(
            consumer="health_scorer",
            error_type="validation",
            symbol="ADAUSDT",
            timeframe="1h",
        )._value.get()
        with caplog.at_level("WARNING"):
            metrics = await scorer.calculate_health("ADAUSDT", "1h")

        assert metrics.quality_score == 0.0
        assert (
            HEALTH_METRICS_VALIDATION_ERRORS.labels(
                symbol="ADAUSDT", timeframe="1h"
            )._value.get()
            == before + 1
        )
        assert (
            THIN_DATA_CONSUMER_ERRORS.labels(
                consumer="health_scorer",
                error_type="validation",
                symbol="ADAUSDT",
                timeframe="1h",
            )._value.get()
            == evidence_before + 1
        )
        assert "data_health_metrics_validation_error" in caplog.text

    @pytest.mark.asyncio
    async def test_repository_error_does_not_emit_validation_evidence(self, caplog):
        db_manager = _make_db_manager()
        scorer = HealthScorer(db_manager)
        scorer.candle_repo.count = AsyncMock(
            side_effect=RuntimeError("database unavailable")
        )
        before = HEALTH_METRICS_VALIDATION_ERRORS.labels(
            symbol="ADAUSDT", timeframe="1h"
        )._value.get()
        evidence_before = THIN_DATA_CONSUMER_ERRORS.labels(
            consumer="health_scorer",
            error_type="validation",
            symbol="ADAUSDT",
            timeframe="1h",
        )._value.get()

        with caplog.at_level("ERROR"):
            metrics = await scorer.calculate_health("ADAUSDT", "1h")

        assert metrics.quality_score == 0.0
        assert (
            HEALTH_METRICS_VALIDATION_ERRORS.labels(
                symbol="ADAUSDT", timeframe="1h"
            )._value.get()
            == before
        )
        assert (
            THIN_DATA_CONSUMER_ERRORS.labels(
                consumer="health_scorer",
                error_type="validation",
                symbol="ADAUSDT",
                timeframe="1h",
            )._value.get()
            == evidence_before
        )
        assert "Error calculating health" in caplog.text

    @pytest.mark.asyncio
    async def test_duplicate_inflated_count_does_not_raise(self):
        """actual_count > expected_count (duplicate timestamp) must not
        raise a pydantic ValidationError, and must not fall back to the
        all-zero default metrics.
        """
        db_manager = _make_db_manager()
        scorer = HealthScorer(db_manager)

        # 1d timeframe over a 24h lookback expects exactly 1 record; a
        # duplicate timestamp makes candle_repo.count report 2.
        scorer.candle_repo.count = AsyncMock(return_value=2)
        scorer.candle_repo.get_latest = AsyncMock(
            return_value=[{"timestamp": datetime.now(UTC) - timedelta(seconds=30)}]
        )
        scorer.health_repo.insert = AsyncMock(return_value=True)

        metrics = await scorer.calculate_health(
            "XRPUSDT", "1d", lookback_hours=24, duplicates_count=1
        )

        assert metrics.completeness == 100.0
        assert metrics.quality_score <= 100.0
        assert metrics.duplicates_count == 1
        # Not the all-zero fallback the try/except block returns on error.
        assert metrics.quality_score > 0.0

    @pytest.mark.asyncio
    async def test_normal_completeness_is_unaffected(self):
        """Sanity check: a non-inflated count keeps its real completeness
        percentage rather than always clamping to 100.
        """
        db_manager = _make_db_manager()
        scorer = HealthScorer(db_manager)

        scorer.candle_repo.count = AsyncMock(return_value=1)
        scorer.candle_repo.get_latest = AsyncMock(
            return_value=[{"timestamp": datetime.now(UTC) - timedelta(seconds=30)}]
        )
        scorer.health_repo.insert = AsyncMock(return_value=True)

        before = HEALTH_METRICS_VALIDATION_CHECKS.labels(
            symbol="XRPUSDT", timeframe="1d"
        )._value.get()
        evidence_before = THIN_DATA_CONSUMER_ERRORS.labels(
            consumer="health_scorer",
            error_type="validation",
            symbol="XRPUSDT",
            timeframe="1d",
        )._value.get()
        metrics = await scorer.calculate_health(
            "XRPUSDT", "1d", lookback_hours=24, duplicates_count=0
        )

        assert metrics.completeness == 100.0
        assert metrics.quality_score <= 100.0
        assert (
            HEALTH_METRICS_VALIDATION_CHECKS.labels(
                symbol="XRPUSDT", timeframe="1d"
            )._value.get()
            == before + 1
        )
        assert (
            THIN_DATA_CONSUMER_ERRORS.labels(
                consumer="health_scorer",
                error_type="validation",
                symbol="XRPUSDT",
                timeframe="1d",
            )._value.get()
            == evidence_before
        )
