from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from data_manager.maintenance.candle_readiness import evaluate_collection


def _metric_value(name: str, pair: str = "BTCUSDT", timeframe: str = "1m") -> float:
    value = REGISTRY.get_sample_value(
        name, labels={"pair": pair, "timeframe": timeframe}
    )
    assert value is not None
    return value


@pytest.mark.asyncio
async def test_readiness_metrics_export_values_and_labels():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    adapter = MagicMock()
    adapter.get_record_count = AsyncMock(return_value=275)
    adapter.query_latest = AsyncMock(
        return_value=[{"timestamp": now - timedelta(seconds=42)}]
    )

    verdict = await evaluate_collection(
        adapter,
        "BTCUSDT",
        "1m",
        required_count=400,
        freshness_intervals=3,
        now=now,
    )

    assert verdict.ready is False
    assert _metric_value("data_manager_candle_readiness_depth") == 275
    assert _metric_value("data_manager_candle_readiness_required_depth") == 400
    assert _metric_value("data_manager_candle_readiness_age_seconds") == 42
    assert _metric_value("data_manager_candle_readiness_max_age_seconds") == 180


@pytest.mark.asyncio
async def test_readiness_metrics_use_unavailable_age_sentinel_on_query_failure():
    adapter = MagicMock()
    adapter.get_record_count = AsyncMock(side_effect=RuntimeError("Mongo unavailable"))
    adapter.query_latest = AsyncMock()

    verdict = await evaluate_collection(
        adapter,
        "ETHUSDT",
        "5m",
        required_count=400,
        freshness_intervals=3,
    )

    assert verdict.ready is False
    assert _metric_value("data_manager_candle_readiness_depth", "ETHUSDT", "5m") == 0
    assert (
        _metric_value("data_manager_candle_readiness_required_depth", "ETHUSDT", "5m")
        == 400
    )
    assert _metric_value("data_manager_candle_readiness_age_seconds", "ETHUSDT", "5m") == -1
    assert (
        _metric_value("data_manager_candle_readiness_max_age_seconds", "ETHUSDT", "5m")
        == 900
    )
