"""
Seasonality and cyclical pattern calculator.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from scipy.fft import fft, fftfreq
from scipy.stats import entropy

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository
from data_manager.models.analytics import MetricMetadata, SeasonalityMetrics

logger = logging.getLogger(__name__)


class SeasonalityCalculator:
    """Calculates seasonality and cyclical patterns from candle data."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize seasonality calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_seasonality(
        self,
        symbol: str,
        timeframe: str,
        window_days: int = 90,
    ) -> SeasonalityMetrics | None:
        """
        Calculate seasonality and cyclical patterns for a symbol.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            window_days: Window size in days

        Returns:
            SeasonalityMetrics or None if insufficient data
        """
        try:
            # Fetch candles from MongoDB (need longer history for seasonality)
            end = datetime.utcnow()
            start = end - timedelta(days=window_days)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 100:
                logger.warning(
                    f"Insufficient data for seasonality calculation: {len(candles)} candles"
                )
                return None

            # Convert to DataFrame
            df = pd.DataFrame(candles)
            df["close"] = df["close"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )
            df["volume"] = df["volume"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )

            # Ensure timestamp is datetime
            df["timestamp"] = pd.to_datetime(df["timestamp"])

            # Extract time components
            df["hour"] = df["timestamp"].dt.hour
            df["day_of_week"] = df["timestamp"].dt.dayofweek

            # Hourly pattern (0-23)
            hourly_pattern = {}
            for hour in range(24):
                hour_data = df[df["hour"] == hour]["close"]
                hourly_pattern[hour] = (
                    Decimal(str(hour_data.mean()))
                    if len(hour_data) > 0
                    else Decimal("0")
                )

            # Daily pattern (0-6: Monday-Sunday)
            daily_pattern = {}
            for day in range(7):
                day_data = df[df["day_of_week"] == day]["close"]
                daily_pattern[day] = (
                    Decimal(str(day_data.mean())) if len(day_data) > 0 else Decimal("0")
                )

            # Seasonal deviation (current vs seasonal average)
            current_hour = datetime.utcnow().hour
            seasonal_avg = float(hourly_pattern.get(current_hour, Decimal("0")))
            current_price = df["close"].iloc[-1]
            seasonal_deviation = (
                Decimal(str((current_price - seasonal_avg) / seasonal_avg * 100))
                if seasonal_avg > 0
                else Decimal("0")
            )

            # Fourier analysis for cycle detection
            prices = df["close"].values
            fft_values = fft(prices)
            frequencies = fftfreq(len(prices))

            # Power spectrum
            power_spectrum = np.abs(fft_values) ** 2

            # Find dominant cycle (peak frequency, excluding DC component)
            positive_freqs = frequencies[1 : len(frequencies) // 2]
            positive_power = power_spectrum[1 : len(power_spectrum) // 2]

            if len(positive_power) > 0:
                dominant_freq_idx = np.argmax(positive_power)
                dominant_freq = positive_freqs[dominant_freq_idx]
                dominant_cycle = int(1 / dominant_freq) if dominant_freq != 0 else None
            else:
                dominant_cycle = None

            # Entropy index (randomness measure)
            hist, _ = np.histogram(df["close"], bins=50, density=True)
            hist = hist[hist > 0]  # Remove zeros for entropy calculation
            entropy_index = Decimal(str(entropy(hist)))

            # Create metadata
            metadata = MetricMetadata(
                method="fourier_analysis",
                window=f"{window_days}d",
                parameters={"fft_size": len(prices)},
                completeness=100.0,
                computed_at=datetime.utcnow(),
            )

            # Create metrics object
            metrics = SeasonalityMetrics(
                symbol=symbol,
                timeframe=timeframe,
                hourly_pattern=hourly_pattern,
                daily_pattern=daily_pattern,
                seasonal_deviation=seasonal_deviation,
                entropy_index=entropy_index,
                dominant_cycle=dominant_cycle,
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_seasonality"
            await self.db_manager.mongodb_adapter.write([metrics], collection)

            logger.info(
                f"Seasonality calculated for {symbol} {timeframe}: "
                f"dominant_cycle={dominant_cycle}, entropy={float(entropy_index):.4f}"
            )

            return metrics

        except Exception as e:
            logger.error(
                f"Error calculating seasonality for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
            return None
