"""
Analytics scheduler for periodic metric computation.
"""

import asyncio
import logging

import constants
from data_manager.analytics.correlation import CorrelationCalculator
from data_manager.analytics.deviation import DeviationCalculator
from data_manager.analytics.regime import RegimeClassifier
from data_manager.analytics.seasonality import SeasonalityCalculator
from data_manager.analytics.spread import SpreadCalculator
from data_manager.analytics.trend import TrendCalculator
from data_manager.analytics.volatility import VolatilityCalculator
from data_manager.analytics.volume import VolumeCalculator
from data_manager.db.database_manager import DatabaseManager

logger = logging.getLogger(__name__)


class AnalyticsScheduler:
    """
    Schedules and orchestrates periodic analytics computation.

    Runs metric calculators for all symbols and timeframes.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize analytics scheduler.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.volatility_calc = VolatilityCalculator(db_manager)
        self.volume_calc = VolumeCalculator(db_manager)
        self.spread_calc = SpreadCalculator(db_manager)
        self.trend_calc = TrendCalculator(db_manager)
        self.deviation_calc = DeviationCalculator(db_manager)
        self.seasonality_calc = SeasonalityCalculator(db_manager)
        self.correlation_calc = CorrelationCalculator(db_manager)
        self.regime_classifier = RegimeClassifier(db_manager)
        self.running = False

    async def start(self) -> None:
        """Start the analytics scheduler."""
        self.running = True
        logger.info("Analytics scheduler started")

        while self.running:
            try:
                await self.run_analytics_cycle()
                await asyncio.sleep(constants.ANALYTICS_INTERVAL)
            except Exception as e:
                logger.warning(
                    f"Analytics cycle failed: {e}. Will retry in {constants.ANALYTICS_INTERVAL}s"
                )
                await asyncio.sleep(30)  # Short backoff on error

        logger.info("Analytics scheduler stopped")

    async def stop(self) -> None:
        """Stop the analytics scheduler."""
        self.running = False

    async def run_analytics_cycle(self) -> None:
        """Run a single analytics cycle for all symbols and timeframes."""
        logger.info("Starting analytics cycle")

        metrics_calculated = 0

        # Calculate analytics for each supported symbol
        for symbol in constants.SUPPORTED_PAIRS:
            # Focus on higher timeframes for analytics (1h, 1d)
            for timeframe in ["1h", "1d"]:
                try:
                    # Calculate volatility
                    volatility = await self.volatility_calc.calculate_volatility(
                        symbol, timeframe, window_days=30
                    )
                    if volatility:
                        metrics_calculated += 1

                    # Calculate volume
                    volume = await self.volume_calc.calculate_volume(
                        symbol, timeframe, window_hours=24
                    )
                    if volume:
                        metrics_calculated += 1

                    # Calculate trend
                    trend = await self.trend_calc.calculate_trend(
                        symbol, timeframe, window_days=30
                    )
                    if trend:
                        metrics_calculated += 1

                    # Calculate deviation
                    deviation = await self.deviation_calc.calculate_deviation(
                        symbol, timeframe, window_days=30
                    )
                    if deviation:
                        metrics_calculated += 1

                    # Calculate seasonality (only for 1h timeframe to avoid overload)
                    if timeframe == "1h":
                        seasonality = await self.seasonality_calc.calculate_seasonality(
                            symbol, timeframe, window_days=90
                        )
                        if seasonality:
                            metrics_calculated += 1

                except Exception as e:
                    logger.warning(
                        f"Error calculating analytics for {symbol} {timeframe}: {e}"
                    )

            # Calculate spread (uses latest depth, not timeframe-specific)
            try:
                spread = await self.spread_calc.calculate_spread(symbol)
                if spread:
                    metrics_calculated += 1
            except Exception as e:
                logger.warning(f"Error calculating spread for {symbol}: {e}")

            # Classify market regime (uses computed metrics)
            try:
                regime = await self.regime_classifier.classify_regime(symbol, "1h")
                if regime:
                    metrics_calculated += 1
            except Exception as e:
                logger.warning(f"Error classifying regime for {symbol}: {e}")

        # Calculate cross-market correlation (all pairs together, 1h timeframe)
        try:
            correlations = await self.correlation_calc.calculate_correlation(
                constants.SUPPORTED_PAIRS, "1h", window_days=30
            )
            if correlations:
                metrics_calculated += len(correlations)
        except Exception as e:
            logger.warning(f"Error calculating correlations: {e}")

        logger.info(
            f"Analytics cycle complete: calculated {metrics_calculated} metrics"
        )
