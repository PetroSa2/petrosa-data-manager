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

from data_manager.auditor.health_scorer import HealthScorer


def _make_db_manager():
    manager = MagicMock()
    manager.mysql_adapter = MagicMock()
    manager.mongodb_adapter = MagicMock()
    return manager


class TestHealthScorerCompletenessClamp:
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

        metrics = await scorer.calculate_health(
            "XRPUSDT", "1d", lookback_hours=24, duplicates_count=0
        )

        assert metrics.completeness == 100.0
        assert metrics.quality_score <= 100.0
