"""
Volatility metrics calculator.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository
from data_manager.models.analytics import MetricMetadata, VolatilityMetrics

logger = logging.getLogger(__name__)


class VolatilityCalculator:
    """Calculates volatility metrics from candle data."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize volatility calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_volatility(
        self,
        symbol: str,
        timeframe: str,
        window_days: int = 30,
    ) -> VolatilityMetrics | None:
        """
        Calculate volatility metrics for a symbol.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            window_days: Window size in days

        Returns:
            VolatilityMetrics or None if insufficient data
        """
        try:
            # Fetch candles from MongoDB
            end = datetime.utcnow()
            start = end - timedelta(days=window_days)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 20:  # Need minimum data points
                logger.warning(
                    f"Insufficient data for volatility calculation: {len(candles)} candles"
                )
                return None

            # Convert to DataFrame
            df = pd.DataFrame(candles)

            # Convert Decimal to float for calculations
            df["close"] = df["close"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )
            df["high"] = df["high"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )
            df["low"] = df["low"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )

            # Calculate returns
            df["returns"] = df["close"].pct_change()

            # Rolling StdDev of returns
            rolling_window = min(20, len(df) - 1)
            rolling_stddev = df["returns"].rolling(window=rolling_window).std().iloc[-1]

            # Annualized Volatility (assume 252 trading days per year)
            annualized_volatility = rolling_stddev * np.sqrt(252)

            # Parkinson volatility (high-low range based)
            df["hl_ratio"] = np.log(df["high"] / df["low"])
            parkinson_series = np.sqrt(
                (1 / (4 * np.log(2)))
                * (df["hl_ratio"] ** 2).rolling(window=rolling_window).mean()
            )
            parkinson = parkinson_series.iloc[-1] if len(parkinson_series) > 0 else None

            # Garman-Klass volatility (OHLC based)
            df["open"] = df["open"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )
            df["log_hl"] = (np.log(df["high"]) - np.log(df["low"])) ** 2
            df["log_co"] = (np.log(df["close"]) - np.log(df["open"])) ** 2
            gk_series = np.sqrt(
                (0.5 * df["log_hl"] - (2 * np.log(2) - 1) * df["log_co"])
                .rolling(window=rolling_window)
                .mean()
            )
            garman_klass = gk_series.iloc[-1] if len(gk_series) > 0 else None

            # Volatility of Volatility
            df["rolling_vol"] = df["returns"].rolling(window=rolling_window).std()
            vov = df["rolling_vol"].rolling(window=rolling_window).std().iloc[-1]

            # Create metadata
            metadata = MetricMetadata(
                method="rolling_stddev",
                window=f"{window_days}d",
                parameters={"rolling_window": rolling_window},
                completeness=len(candles) / (window_days * 24) * 100,  # Approximate
                computed_at=datetime.utcnow(),
            )

            # Create metrics object
            metrics = VolatilityMetrics(
                symbol=symbol,
                timeframe=timeframe,
                rolling_stddev=(
                    Decimal(str(rolling_stddev))
                    if not np.isnan(rolling_stddev)
                    else Decimal("0")
                ),
                annualized_volatility=(
                    Decimal(str(annualized_volatility))
                    if not np.isnan(annualized_volatility)
                    else Decimal("0")
                ),
                parkinson=(
                    Decimal(str(parkinson))
                    if parkinson and not np.isnan(parkinson)
                    else None
                ),
                garman_klass=(
                    Decimal(str(garman_klass))
                    if garman_klass and not np.isnan(garman_klass)
                    else None
                ),
                volatility_of_volatility=Decimal(str(vov))
                if not np.isnan(vov)
                else None,
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_volatility"
            await self.db_manager.mongodb_adapter.write([metrics], collection)

            logger.info(
                f"Volatility calculated for {symbol} {timeframe}: "
                f"stddev={rolling_stddev:.6f}, annualized={annualized_volatility:.6f}"
            )

            return metrics

        except Exception as e:
            logger.error(
                f"Error calculating volatility for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
            return None
