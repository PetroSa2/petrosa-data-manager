"""
Tests for analytics base calculator backfill-trigger capability.

Covers:
* ``BaseCalculator._insufficient_data`` emits the Prometheus counter
  even when backfill is disabled.
* When ``ANALYTICS_BACKFILL_ON_INSUFFICIENT=true`` and a
  ``BackfillTrigger`` is wired, on_verdict is called.
* When the backfill trigger is not wired the method is a no-op
  (no crash).
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Force the env-var before any imports
os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "false"

from data_manager.analytics.base import BaseCalculator, analytics_backfill_triggered
from data_manager.models.events import BackfillRequest


def _make_db() -> MagicMock:
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    return db


class TestCandleSeconds:
    """_candle_seconds returns correct values for common timeframes."""

    @pytest.mark.parametrize(
        "tf,expected",
        [
            ("1m", 60),
            ("5m", 300),
            ("15m", 900),
            ("1h", 3600),
            ("4h", 14400),
            ("1d", 86400),
            ("1w", 604800),
        ],
    )
    def test_known_timeframes(self, tf, expected):
        calc = BaseCalculator(_make_db())
        assert calc._candle_seconds(tf) == expected

    def test_unknown_timeframe_falls_back_to_3600(self):
        calc = BaseCalculator(_make_db())
        assert calc._candle_seconds("2d") == 3600


class TestBackfillTriggerIntegration:
    """End-to-end: calculator → backfill trigger → on_verdict call."""

    @pytest.mark.asyncio
    async def test_on_verdict_called_when_env_enabled(self):
        """When ANALYTICS_BACKFILL_ON_INSUFFICIENT=true and trigger wired,
        on_verdict must be called."""
        os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "true"
        try:
            calc = BaseCalculator(_make_db())
            trigger = AsyncMock()
            trigger.on_verdict = AsyncMock(return_value=[])
            calc.backfill_trigger = trigger

            await calc._insufficient_data(
                "BTCUSDT", "1h", 2, 50, 30, calculator_name="test_calc"
            )

            trigger.on_verdict.assert_called_once()
            call_args = trigger.on_verdict.call_args
            assert call_args[0][0] == "unhealthy"
            assert "test_calc insufficient data" in call_args[0][1]
            assert "BTCUSDT" in call_args[0][1]
            assert "1h" in call_args[0][1]
        finally:
            os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "false"

    @pytest.mark.asyncio
    async def test_no_crash_when_trigger_not_wired(self):
        """When trigger is None, _insufficient_data must not raise."""
        os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "true"
        try:
            calc = BaseCalculator(_make_db())
            calc.backfill_trigger = None
            # Should not raise
            await calc._insufficient_data(
                "BTCUSDT", "1h", 2, 50, 30, calculator_name="test_calc"
            )
        finally:
            os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "false"

    @pytest.mark.asyncio
    async def test_no_crash_when_env_disabled(self):
        """When env-var is false, on_verdict must NOT be called."""
        os.environ["ANALYTICS_BACKFILL_ON_INSUFFICIENT"] = "false"
        calc = BaseCalculator(_make_db())
        trigger = AsyncMock()
        trigger.on_verdict = AsyncMock(return_value=[])
        calc.backfill_trigger = trigger

        await calc._insufficient_data(
            "BTCUSDT", "1h", 2, 50, 30, calculator_name="test_calc"
        )

        trigger.on_verdict.assert_not_called()

    @pytest.mark.asyncio
    async def test_counter_increments_on_insufficient_data(self):
        """The Prometheus counter must always be incremented."""
        calc = BaseCalculator(_make_db())
        before = analytics_backfill_triggered._metrics.get(
            ("test_calc", "BTCUSDT", "1h"), 0
        )
        if isinstance(before, int):
            before_val = before
        else:
            before_val = before._value.get()
        await calc._insufficient_data(
            "BTCUSDT", "1h", 2, 50, 30, calculator_name="test_calc"
        )
        after = analytics_backfill_triggered._metrics.get(
            ("test_calc", "BTCUSDT", "1h"), 0
        )
        if isinstance(after, int):
            after_val = after
        else:
            after_val = after._value.get()
        assert after_val > before_val
