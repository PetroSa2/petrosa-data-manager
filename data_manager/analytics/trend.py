"""
Trend and momentum indicators calculator.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository
from data_manager.models.analytics import MetricMetadata, TrendMetrics

logger = logging.getLogger(__name__)


class TrendCalculator:
    """Calculates trend and momentum indicators from candle data."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize trend calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_trend(
        self,
        symbol: str,
        timeframe: str,
        window_days: int = 30,
    ) -> TrendMetrics | None:
        """
        Calculate trend and momentum indicators for a symbol.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            window_days: Window size in days

        Returns:
            TrendMetrics or None if insufficient data
        """
        try:
            # Fetch candles from MongoDB
            end = datetime.utcnow()
            start = end - timedelta(days=window_days)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 50:  # Need minimum data points
                logger.warning(
                    f"Insufficient data for trend calculation: {len(candles)} candles"
                )
                return None

            # Convert to DataFrame
            df = pd.DataFrame(candles)

            # Convert Decimal to float
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].apply(
                    lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
                )

            # Calculate moving averages
            df["sma_20"] = df["close"].rolling(window=20).mean()
            df["ema_20"] = df["close"].ewm(span=20).mean()

            # Weighted Moving Average (WMA)
            def weighted_average(prices):
                weights = np.arange(1, len(prices) + 1)
                return (prices * weights).sum() / weights.sum()

            df["wma_20"] = (
                df["close"].rolling(window=20).apply(weighted_average, raw=True)
            )

            # Rate of Change (ROC) - 10 periods
            df["roc"] = df["close"].pct_change(periods=10) * 100

            # Directional Strength (percentage of up candles)
            df["up"] = (df["close"] > df["open"]).astype(int)
            directional_strength = df["up"].rolling(window=20).mean() * 100

            # Crossover Detection (SMA 20 vs SMA 50)
            df["sma_50"] = df["close"].rolling(window=50).mean()
            crossover_signal = (
                "bullish"
                if df["sma_20"].iloc[-1] > df["sma_50"].iloc[-1]
                else "bearish"
            )

            # RSI (Relative Strength Index) - 14 periods (calculated but not used yet)
            # delta = df["close"].diff()
            # gain = delta.where(delta > 0, 0).rolling(window=14).mean()
            # loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            # rs = gain / loss
            # rsi = 100 - (100 / (1 + rs))

            # Rolling Beta to benchmark (simplified - would need BTCUSDT data)
            # For now, use simple correlation as proxy
            rolling_beta = None  # TODO: Fetch benchmark and calculate

            # Create metadata
            metadata = MetricMetadata(
                method="moving_averages",
                window=f"{window_days}d",
                parameters={"sma_window": 20, "ema_span": 20},
                completeness=len(candles) / (window_days * 24) * 100,
                computed_at=datetime.utcnow(),
            )

            # Create metrics object
            metrics = TrendMetrics(
                symbol=symbol,
                timeframe=timeframe,
                sma=(
                    Decimal(str(df["sma_20"].iloc[-1]))
                    if not pd.isna(df["sma_20"].iloc[-1])
                    else Decimal("0")
                ),
                ema=(
                    Decimal(str(df["ema_20"].iloc[-1]))
                    if not pd.isna(df["ema_20"].iloc[-1])
                    else Decimal("0")
                ),
                wma=(
                    Decimal(str(df["wma_20"].iloc[-1]))
                    if not pd.isna(df["wma_20"].iloc[-1])
                    else Decimal("0")
                ),
                rate_of_change=(
                    Decimal(str(df["roc"].iloc[-1]))
                    if not pd.isna(df["roc"].iloc[-1])
                    else Decimal("0")
                ),
                directional_strength=(
                    Decimal(str(directional_strength.iloc[-1]))
                    if not pd.isna(directional_strength.iloc[-1])
                    else Decimal("0")
                ),
                crossover_signal=crossover_signal,
                rolling_beta=rolling_beta,
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_trend"
            await self.db_manager.mongodb_adapter.write([metrics], collection)

            logger.info(
                f"Trend calculated for {symbol} {timeframe}: "
                f"sma={float(metrics.sma):.2f}, roc={float(metrics.rate_of_change):.2f}%"
            )

            return metrics

        except Exception as e:
            logger.error(
                f"Error calculating trend for {symbol} {timeframe}: {e}", exc_info=True
            )
            return None
