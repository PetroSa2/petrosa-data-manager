"""
Base analytics calculator with backfill-trigger capability.

Every analytics calculator inherits from ``BaseCalculator`` which:

* Detects whether ``ANALYTICS_BACKFILL_ON_INSUFFICIENT`` is enabled
  (env-var, default ``false``).
* Emits the ``data_manager_analytics_backfill_triggered_total`` counter
  when a calculator hits insufficient data and backfill is enabled.
* Optionally delegates to the ``BackfillTrigger`` bridge (if one is
  wired in at the scheduler level) so that the missing range is
  requested from the backfill orchestrator.

Usage
-----

.. code-block:: python

    class TrendCalculator(BaseCalculator):
        async def calculate_trend(self, symbol, timeframe, window_days=30):
            candles = await self.candle_repo.get_range(...)
            if len(candles) < 50:
                self._insufficient_data(symbol, timeframe, len(candles), 50, window_days)
                return None
            ...

"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter

logger = logging.getLogger(__name__)

# Prometheus counter: data_manager_analytics_backfill_triggered_total
analytics_backfill_triggered = Counter(
    "data_manager_analytics_backfill_triggered_total",
    "Number of analytics backfill requests triggered by calculators "
    "due to insufficient data (per calculator type)",
    ["calculator", "symbol", "timeframe"],
)


def _insufficient_backfill_enabled() -> bool:
    """Check whether backfill on insufficient data is enabled.

    Reads the env-var at call time so tests can toggle it without
    needing to reload the module (which conflicts with Prometheus's
    global metric registry).
    """
    return os.getenv("ANALYTICS_BACKFILL_ON_INSUFFICIENT", "false").lower() in (
        "1",
        "true",
        "yes",
    )


class BaseCalculator:
    """Shared base for all analytics calculators."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager
        self.candle_repo: object | None = None
        # Lazily-loaded backfill trigger
        self._backfill_trigger: object | None = None

    # ------------------------------------------------------------------
    # Backfill trigger API
    # ------------------------------------------------------------------

    @property
    def backfill_trigger(self) -> object | None:
        """Return the wired BackfillTrigger, if any."""
        return self._backfill_trigger

    @backfill_trigger.setter
    def backfill_trigger(self, value: object | None) -> None:
        self._backfill_trigger = value

    async def _insufficient_data(
        self,
        symbol: str,
        timeframe: str,
        actual: int,
        needed: int,
        window_days: int,
        *,
        calculator_name: str | None = None,
    ) -> None:
        """Handle insufficient data: log, emit metric, optionally trigger backfill.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``BTCUSDT``.
        timeframe:
            Candle timeframe, e.g. ``1h``.
        actual:
            Number of candles retrieved.
        needed:
            Minimum candles required.
        window_days:
            Lookback window in days (used to compute backfill range).
        calculator_name:
            Human-readable calculator name for the metric label.
            Defaults to the subclass name.
        """
        if calculator_name is None:
            calculator_name = type(self).__name__

        gap_seconds = max(
            int(window_days * 86400) - (actual * self._candle_seconds(timeframe)), 0
        )
        gap_seconds = max(
            gap_seconds, int((needed - actual) * self._candle_seconds(timeframe))
        )

        logger.warning(
            "Insufficient data for %s calculation: %d candles (need %d+) "
            "for %s %s — gap ~%ds",
            calculator_name,
            actual,
            needed,
            symbol,
            timeframe,
            gap_seconds,
        )

        # Always emit the metric (regardless of env-var gate).
        analytics_backfill_triggered.labels(
            calculator=calculator_name,
            symbol=symbol,
            timeframe=timeframe,
        ).inc()

        if not _insufficient_backfill_enabled():
            return

        if self._backfill_trigger is None:
            logger.debug(
                "ANALYTICS_BACKFILL_ON_INSUFFICIENT=true but no "
                "BackfillTrigger wired for %s; skipping backfill request",
                calculator_name,
            )
            return

        gap_end = datetime.now(UTC)
        gap_start = gap_end - timedelta(seconds=max(gap_seconds, 3600))

        try:
            from data_manager.models.events import BackfillRequest

            request = BackfillRequest(
                symbol=symbol,
                data_type="candles",
                timeframe=timeframe,
                start_time=gap_start,
                end_time=gap_end,
                priority=3,
                source=f"calculator_{calculator_name}",
            )
            # Fire-and-forget: let the trigger handle dedup/cooldown.
            await self._backfill_trigger.on_verdict(
                "unhealthy",
                f"{calculator_name} insufficient data: {actual} < {needed} "
                f"for {symbol} {timeframe}",
                auditor_only=False,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to trigger backfill for %s %s: %s",
                calculator_name,
                symbol,
                timeframe,
                exc_info=True,
            )

    def _candle_seconds(self, timeframe: str) -> int:
        """Return approximate seconds per candle for *timeframe*."""
        mapping = {
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "6h": 21600,
            "8h": 28800,
            "12h": 43200,
            "1d": 86400,
            "1w": 604800,
        }
        return mapping.get(timeframe, 3600)
