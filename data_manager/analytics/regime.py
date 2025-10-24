"""
Market regime classifier.
"""

import logging
from datetime import datetime
from decimal import Decimal

from data_manager.db.database_manager import DatabaseManager
from data_manager.models.analytics import MarketRegime, MetricMetadata

logger = logging.getLogger(__name__)


class RegimeClassifier:
    """Classifies market conditions based on computed metrics."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize regime classifier.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager

    async def classify_regime(self, symbol: str, timeframe: str) -> MarketRegime | None:
        """
        Classify market regime for a symbol.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')

        Returns:
            MarketRegime or None if insufficient data
        """
        try:
            # Fetch recent metrics from MongoDB
            volatility_data = await self.db_manager.mongodb_adapter.query_latest(
                f"analytics_{symbol}_volatility", symbol=symbol, limit=1
            )
            volume_data = await self.db_manager.mongodb_adapter.query_latest(
                f"analytics_{symbol}_volume", symbol=symbol, limit=1
            )
            trend_data = await self.db_manager.mongodb_adapter.query_latest(
                f"analytics_{symbol}_trend", symbol=symbol, limit=1
            )

            if not volatility_data or not volume_data:
                logger.warning(
                    f"Insufficient metrics for regime classification: {symbol}"
                )
                return None

            # Extract metric values
            annualized_vol = float(volatility_data[0].get("annualized_volatility", 0))
            volume_spike_ratio = float(volume_data[0].get("volume_spike_ratio", 1.0))
            roc = 0.0  # Rate of change from trend data if available
            if trend_data:
                roc = float(trend_data[0].get("rate_of_change", 0))

            # Define thresholds (can be calibrated based on historical percentiles)
            vol_high = annualized_vol > 0.5  # 50% annualized volatility
            vol_low = annualized_vol < 0.2  # 20% annualized volatility

            volume_high = volume_spike_ratio > 1.5  # 50% above baseline
            volume_low = volume_spike_ratio < 0.7  # 30% below baseline

            trend_bullish = roc > 2.0  # 2% positive rate of change
            trend_bearish = roc < -2.0  # 2% negative rate of change

            # Classify regime based on conditions
            regime = "unknown"
            confidence = 0.5
            volatility_level = "medium"
            volume_level = "medium"
            trend_direction = "neutral"

            # Determine volatility level
            if vol_high:
                volatility_level = "high"
            elif vol_low:
                volatility_level = "low"

            # Determine volume level
            if volume_high:
                volume_level = "high"
            elif volume_low:
                volume_level = "low"

            # Determine trend direction
            if trend_bullish:
                trend_direction = "bullish"
            elif trend_bearish:
                trend_direction = "bearish"

            # Classify regime
            if vol_high and volume_low:
                regime = "turbulent_illiquidity"
                confidence = 0.8
            elif vol_low and volume_high:
                regime = "stable_accumulation"
                confidence = 0.85
            elif vol_high and volume_high:
                regime = "breakout_phase"
                confidence = 0.9
            elif vol_low and volume_low:
                regime = "consolidation"
                confidence = 0.75
            elif vol_high and volume_high and trend_bullish:
                regime = "bullish_acceleration"
                confidence = 0.85
            elif vol_high and volume_high and trend_bearish:
                regime = "bearish_acceleration"
                confidence = 0.85
            elif volatility_level == "medium" and volume_level == "medium":
                regime = "balanced_market"
                confidence = 0.7
            else:
                regime = "transitional"
                confidence = 0.6

            # Create metadata
            metadata = MetricMetadata(
                method="threshold_classification",
                window="latest",
                parameters={
                    "vol_high_threshold": 0.5,
                    "vol_low_threshold": 0.2,
                    "volume_high_threshold": 1.5,
                    "volume_low_threshold": 0.7,
                },
                completeness=100.0,
                computed_at=datetime.utcnow(),
            )

            # Create regime object
            regime_obj = MarketRegime(
                symbol=symbol,
                timeframe=timeframe,
                regime=regime,
                volatility_level=volatility_level,
                volume_level=volume_level,
                trend_direction=trend_direction,
                confidence=Decimal(str(confidence)),
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_regime"
            await self.db_manager.mongodb_adapter.write([regime_obj], collection)

            logger.info(
                f"Regime classified for {symbol} {timeframe}: "
                f"{regime} (confidence={confidence:.2f})"
            )

            return regime_obj

        except Exception as e:
            logger.error(
                f"Error classifying regime for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
            return None
