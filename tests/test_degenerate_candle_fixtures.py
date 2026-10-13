"""Regression coverage for canonical degenerate candle sets."""

import math
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.auditor.gap_detector import GapDetector
from data_manager.auditor.health_scorer import HealthScorer
from data_manager.models.health import DataHealthMetrics
from tests.fixtures.degenerate_candles import DEGENERATE_CANDLE_SETS


def _make_db_manager():
    manager = MagicMock()
    manager.mysql_adapter = MagicMock()
    manager.mongodb_adapter = MagicMock()
    return manager


@pytest.mark.parametrize(
    ("fixture_name", "candles", "defect"),
    DEGENERATE_CANDLE_SETS,
    ids=[fixture[0] for fixture in DEGENERATE_CANDLE_SETS],
)
@pytest.mark.asyncio
async def test_health_scorer_accepts_canonical_degenerate_candles(
    fixture_name, candles, defect
):
    del fixture_name, defect
    scorer = HealthScorer(_make_db_manager())
    scorer.candle_repo.count = AsyncMock(return_value=len(candles))
    scorer.candle_repo.get_latest = AsyncMock(return_value=[candles[-1]])
    scorer.health_repo.insert = AsyncMock(return_value=True)

    metrics = await scorer.calculate_health("BTCUSDT", "5m", lookback_hours=24)

    assert isinstance(metrics, DataHealthMetrics)
    validated = DataHealthMetrics(**metrics.model_dump())
    for value in validated.model_dump().values():
        if isinstance(value, int | float):
            assert math.isfinite(value)
    assert 0.0 <= validated.completeness <= 100.0
    assert 0.0 <= validated.quality_score <= 100.0


@pytest.mark.parametrize(
    ("fixture_name", "candles", "defect"),
    DEGENERATE_CANDLE_SETS,
    ids=[fixture[0] for fixture in DEGENERATE_CANDLE_SETS],
)
@pytest.mark.asyncio
async def test_gap_detector_accepts_canonical_degenerate_candles(
    fixture_name, candles, defect
):
    del fixture_name, defect
    detector = GapDetector(_make_db_manager())
    detector.candle_repo.get_range = AsyncMock(return_value=candles)
    detector.audit_repo.log_gap = AsyncMock(return_value=True)
    start = candles[0]["timestamp"]
    end = candles[-1]["timestamp"] + timedelta(minutes=5)

    gaps = await detector.detect_gaps("BTCUSDT", "5m", start, end)

    assert isinstance(gaps, list)
    for gap in gaps:
        for value in gap.model_dump().values():
            if isinstance(value, int | float):
                assert math.isfinite(value)
