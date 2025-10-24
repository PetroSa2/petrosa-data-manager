"""
Deviation and statistical metrics calculator.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository
from data_manager.models.analytics import DeviationMetrics, MetricMetadata

logger = logging.getLogger(__name__)


class DeviationCalculator:
    """Calculates deviation and statistical metrics from candle data."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize deviation calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_deviation(
        self,
        symbol: str,
        timeframe: str,
        window_days: int = 30,
    ) -> DeviationMetrics | None:
        """
        Calculate deviation and statistical metrics for a symbol.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            window_days: Window size in days

        Returns:
            DeviationMetrics or None if insufficient data
        """
        try:
            # Fetch candles from MongoDB
            end = datetime.utcnow()
            start = end - timedelta(days=window_days)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 20:
                logger.warning(
                    f"Insufficient data for deviation calculation: {len(candles)} candles"
                )
                return None

            # Convert to DataFrame
            df = pd.DataFrame(candles)
            df["close"] = df["close"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )

            # Standard Deviation and Variance
            rolling_window = min(20, len(df) - 1)
            standard_deviation = (
                df["close"].rolling(window=rolling_window).std().iloc[-1]
            )
            variance = df["close"].rolling(window=rolling_window).var().iloc[-1]

            # Bollinger Bands (SMA ± 2*StdDev)
            sma = df["close"].rolling(window=rolling_window).mean()
            std = df["close"].rolling(window=rolling_window).std()
            bollinger_upper = (sma + (2 * std)).iloc[-1]
            bollinger_lower = (sma - (2 * std)).iloc[-1]
            bollinger_middle = sma.iloc[-1]

            # Z-Score (standardized deviation)
            current_price = df["close"].iloc[-1]
            z_score = (
                (current_price - bollinger_middle) / standard_deviation
                if standard_deviation > 0
                else 0
            )

            # Price Range Index
            rolling_max = df["close"].rolling(window=rolling_window).max()
            rolling_min = df["close"].rolling(window=rolling_window).min()
            price_range = rolling_max - rolling_min
            price_range_index = (
                (current_price - rolling_min.iloc[-1]) / price_range.iloc[-1]
                if price_range.iloc[-1] > 0
                else 0.5
            )

            # Autocorrelation (lag-1)
            autocorrelation = df["close"].autocorr(lag=1)

            # Coefficient of Variation (calculated but not used in response yet)
            # cv = (standard_deviation / bollinger_middle) if bollinger_middle > 0 else 0

            # Skewness and Kurtosis (calculated but not used in response yet)
            # skewness = df["close"].rolling(window=rolling_window).skew().iloc[-1]
            # kurtosis = df["close"].rolling(window=rolling_window).kurt().iloc[-1]

            # Create metadata
            metadata = MetricMetadata(
                method="statistical_analysis",
                window=f"{window_days}d",
                parameters={
                    "rolling_window": rolling_window,
                    "bollinger_std_multiplier": 2,
                },
                completeness=100.0,
                computed_at=datetime.utcnow(),
            )

            # Create metrics object
            metrics = DeviationMetrics(
                symbol=symbol,
                timeframe=timeframe,
                standard_deviation=(
                    Decimal(str(standard_deviation))
                    if not np.isnan(standard_deviation)
                    else Decimal("0")
                ),
                variance=Decimal(str(variance))
                if not np.isnan(variance)
                else Decimal("0"),
                z_score=Decimal(str(z_score))
                if not np.isnan(z_score)
                else Decimal("0"),
                bollinger_upper=(
                    Decimal(str(bollinger_upper))
                    if not np.isnan(bollinger_upper)
                    else Decimal("0")
                ),
                bollinger_lower=(
                    Decimal(str(bollinger_lower))
                    if not np.isnan(bollinger_lower)
                    else Decimal("0")
                ),
                price_range_index=(
                    Decimal(str(price_range_index))
                    if not np.isnan(price_range_index)
                    else Decimal("0")
                ),
                autocorrelation=(
                    Decimal(str(autocorrelation))
                    if not np.isnan(autocorrelation)
                    else None
                ),
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_deviation"
            await self.db_manager.mongodb_adapter.write([metrics], collection)

            logger.info(
                f"Deviation calculated for {symbol} {timeframe}: "
                f"stddev={float(standard_deviation):.2f}, z_score={float(z_score):.2f}"
            )

            return metrics

        except Exception as e:
            logger.error(
                f"Error calculating deviation for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
            return None
