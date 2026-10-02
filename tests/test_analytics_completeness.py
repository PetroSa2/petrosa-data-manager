"""Regression tests for analytics completeness metadata."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.analytics.trend import TrendCalculator


def _candle(timestamp: datetime) -> dict:
    return {
        "symbol": "ADAUSDT",
        "timestamp": timestamp,
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100.5"),
        "volume": Decimal("10"),
    }


@pytest.mark.asyncio
async def test_trend_completeness_deduplicates_inclusive_range() -> None:
    db_manager = Mock()
    db_manager.mysql_adapter = Mock()
    db_manager.mongodb_adapter.write = AsyncMock()
    calculator = TrendCalculator(db_manager)

    start = datetime.now(UTC) - timedelta(days=30)
    candles = [_candle(start + timedelta(hours=index)) for index in range(721)]
    candles.append(candles[-1].copy())
    calculator.candle_repo.get_range = AsyncMock(return_value=candles)

    result = await calculator.calculate_trend("ADAUSDT", "1h", window_days=30)

    assert result is not None
    assert result.metadata.completeness == pytest.approx(100.0)
