"""
Market regime classifier.

Confidence of the residual ``transitional`` regime (petrosa-data-manager#534)
---------------------------------------------------------------------------
The rule-based regimes keep their fixed rule confidences (0.70-0.90). The
residual ``transitional`` regime used to report a constant 0.6, so no consumer
could tell a clear mixed reading from one sitting on a threshold. It is now

    confidence = 0.5 + 0.4 * min(clarity_volatility, clarity_volume)

(range 0.5-0.9, rounded to 4 decimals), where the *clarity* of an input is how
far it sits from the threshold that would change its level, in [0, 1]:

* ``medium`` (``low <= x <= high``): ``min(x - low, high - x) / ((high - low) / 2)``,
  0 on a threshold and 1 at the middle of the band;
* ``high`` (``x > high``): ``min(1, (x - high) / high)``;
* ``low`` (``x < low``): ``min(1, (low - x) / low)``;
* a missing or non-finite input has clarity 0.

The weakest input decides: a transitional reading is only as sure as the less
clear of the two levels it rests on. With the CIO's low-confidence cut at 0.70
a transitional regime is therefore confident from a clarity of 0.5.
The classifier also reports how long the current regime has held, from the
stored history, and the raw inputs and thresholds (``inputs`` on the regime).
"""

import logging
import math
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017
from decimal import Decimal

from data_manager.db.database_manager import DatabaseManager
from data_manager.models.analytics import MarketRegime, MetricMetadata

logger = logging.getLogger(__name__)

VOL_HIGH = 0.5  # 50% annualized volatility
VOL_LOW = 0.2  # 20% annualized volatility
VOLUME_HIGH = 1.5  # 50% above baseline
VOLUME_LOW = 0.7  # 30% below baseline
TRANSITIONAL_FLOOR = 0.5
TRANSITIONAL_SPAN = 0.4
#: How many stored observations are read to tell how long the current regime has held.
HOLD_HISTORY = 96


def level_clarity(value: float, low: float, high: float) -> float:
    """How far ``value`` sits from the threshold that would change its level, in [0, 1] (see module doc)."""
    if value is None or not math.isfinite(value):
        return 0.0
    if value > high:
        return min(1.0, (value - high) / high)
    if value < low:
        return min(1.0, (low - value) / low) if low > 0 else 1.0
    return min(value - low, high - value) / ((high - low) / 2)


def transitional_confidence(vol_clarity: float, volume_clarity: float) -> float:
    """Confidence of a ``transitional`` reading: the weaker input decides (see module doc)."""
    clarity = max(0.0, min(1.0, vol_clarity, volume_clarity))
    return round(TRANSITIONAL_FLOOR + TRANSITIONAL_SPAN * clarity, 4)


class RegimeClassifier:
    """Classifies market conditions based on computed metrics."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize regime classifier.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager

    async def _held(self, symbol: str, regime: str) -> tuple[int, datetime | None]:
        """How many consecutive stored observations (this one included) read ``regime``, and since when.

        Best effort: a read failure or an empty history gives (1, None), never an error.
        """
        try:
            history = await self.db_manager.mongodb_adapter.query_latest(
                f"analytics_{symbol}_regime", symbol=symbol, limit=HOLD_HISTORY
            )
        except Exception as exc:
            logger.warning(f"Regime history unavailable for {symbol}: {exc}")
            return 1, None
        held, since = 1, None
        for row in history if isinstance(history, list) else []:
            if not isinstance(row, dict) or row.get("regime") != regime:
                break
            held += 1
            stamp = row.get("timestamp") or (row.get("metadata") or {}).get(
                "computed_at"
            )
            if isinstance(stamp, datetime):
                since = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
        return held, since

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
            vol_high = annualized_vol > VOL_HIGH
            vol_low = annualized_vol < VOL_LOW

            volume_high = volume_spike_ratio > VOLUME_HIGH
            volume_low = volume_spike_ratio < VOLUME_LOW

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
            vol_clarity = level_clarity(annualized_vol, VOL_LOW, VOL_HIGH)
            volume_clarity = level_clarity(volume_spike_ratio, VOLUME_LOW, VOLUME_HIGH)
            if regime == "transitional":
                confidence = transitional_confidence(vol_clarity, volume_clarity)
            held_observations, held_since = await self._held(symbol, regime)
            computed_at = datetime.now(UTC)
            inputs = {
                "annualized_volatility": annualized_vol,
                "volume_spike_ratio": volume_spike_ratio,
                "rate_of_change": roc,
                "thresholds": {
                    "vol_high": VOL_HIGH,
                    "vol_low": VOL_LOW,
                    "volume_high": VOLUME_HIGH,
                    "volume_low": VOLUME_LOW,
                },
                "clarity": {"volatility": vol_clarity, "volume": volume_clarity},
                "confidence_basis": (
                    "transitional: 0.5 + 0.4 * min(clarity); fixed rule confidence otherwise"
                    if regime == "transitional"
                    else "fixed rule confidence"
                ),
                "held_observations": held_observations,
                "held_since": (held_since or computed_at).isoformat(),
            }

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
                computed_at=computed_at,
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
                inputs=inputs,
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
