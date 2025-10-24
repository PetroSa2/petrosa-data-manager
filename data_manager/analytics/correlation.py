"""
Correlation and cross-market metrics calculator.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from scipy.signal import correlate

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository
from data_manager.models.analytics import CorrelationMetrics, MetricMetadata

logger = logging.getLogger(__name__)


class CorrelationCalculator:
    """Calculates correlation and cross-market metrics."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize correlation calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_correlation(
        self,
        symbols: list[str],
        timeframe: str,
        window_days: int = 30,
    ) -> dict[str, CorrelationMetrics]:
        """
        Calculate correlation metrics for multiple symbols.

        Args:
            symbols: List of trading pair symbols
            timeframe: Timeframe (e.g., '1h', '1d')
            window_days: Window size in days

        Returns:
            Dictionary of symbol -> CorrelationMetrics
        """
        try:
            logger.info(f"Calculating correlations for {len(symbols)} symbols")

            # Fetch candles for all symbols
            end = datetime.utcnow()
            start = end - timedelta(days=window_days)

            all_candles = {}
            for symbol in symbols:
                try:
                    candles = await self.candle_repo.get_range(
                        symbol, timeframe, start, end
                    )
                    if candles and len(candles) > 20:
                        all_candles[symbol] = pd.DataFrame(candles)
                except Exception as e:
                    logger.warning(f"Failed to fetch candles for {symbol}: {e}")

            if len(all_candles) < 2:
                logger.warning("Insufficient symbols with data for correlation")
                return {}

            # Align timestamps and create merged DataFrame
            merged = None
            for symbol, df in all_candles.items():
                df["close"] = df["close"].apply(
                    lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df_pivot = df.set_index("timestamp")[["close"]].rename(
                    columns={"close": symbol}
                )

                if merged is None:
                    merged = df_pivot
                else:
                    merged = merged.join(df_pivot, how="inner")

            if merged is None or len(merged) < 20:
                logger.warning("Insufficient aligned data for correlation")
                return {}

            # Calculate full correlation matrix
            full_corr_matrix = merged.corr()

            # Benchmark (usually BTCUSDT)
            benchmark = "BTCUSDT" if "BTCUSDT" in symbols else symbols[0]

            # Create metrics for each symbol
            results = {}

            for symbol in symbols:
                if symbol not in merged.columns:
                    continue

                # Correlation matrix for this symbol (to all others)
                correlation_matrix = {}
                for other_symbol in symbols:
                    if other_symbol in merged.columns:
                        correlation_matrix[other_symbol] = Decimal(
                            str(full_corr_matrix.loc[symbol, other_symbol])
                        )

                # Rolling correlation to benchmark
                rolling_correlation = Decimal("0")
                if symbol != benchmark and benchmark in merged.columns:
                    rolling_corr_series = (
                        merged[symbol].rolling(window=30).corr(merged[benchmark])
                    )
                    if not rolling_corr_series.empty:
                        rolling_correlation = Decimal(str(rolling_corr_series.iloc[-1]))

                # Cross-correlation lag detection
                cross_correlation_lag = None
                if symbol != benchmark and benchmark in merged.columns:
                    try:
                        # Calculate cross-correlation
                        corr_result = correlate(
                            merged[benchmark].values, merged[symbol].values, mode="same"
                        )
                        lag = int(np.argmax(corr_result) - len(corr_result) // 2)
                        cross_correlation_lag = lag
                    except Exception as e:
                        logger.debug(f"Cross-correlation failed for {symbol}: {e}")

                # Volatility correlation (correlation of volatility series)
                volatility_correlation = None
                if symbol != benchmark and benchmark in merged.columns:
                    try:
                        vol_symbol = merged[symbol].rolling(window=20).std()
                        vol_benchmark = merged[benchmark].rolling(window=20).std()
                        vol_corr = vol_symbol.corr(vol_benchmark)
                        if not np.isnan(vol_corr):
                            volatility_correlation = Decimal(str(vol_corr))
                    except Exception as e:
                        logger.debug(f"Volatility correlation failed for {symbol}: {e}")

                # Create metadata
                metadata = MetricMetadata(
                    method="pearson_correlation",
                    window=f"{window_days}d",
                    parameters={"symbols": len(symbols), "benchmark": benchmark},
                    completeness=100.0,
                    computed_at=datetime.utcnow(),
                )

                # Create metrics object
                metrics = CorrelationMetrics(
                    symbol=symbol,
                    timeframe=timeframe,
                    correlation_matrix=correlation_matrix,
                    rolling_correlation=rolling_correlation,
                    cross_correlation_lag=cross_correlation_lag,
                    volatility_correlation=volatility_correlation,
                    metadata=metadata,
                )

                results[symbol] = metrics

                # Store in MongoDB
                collection = f"analytics_{symbol}_correlation"
                await self.db_manager.mongodb_adapter.write([metrics], collection)

            # Also store the full correlation matrix
            matrix_doc = {
                "timestamp": datetime.utcnow(),
                "timeframe": timeframe,
                "matrix": {
                    symbol: {k: str(v) for k, v in corr.items()}
                    for symbol, corr in full_corr_matrix.to_dict().items()
                },
            }

            class MatrixWrapper:
                def model_dump(self):
                    return matrix_doc

            await self.db_manager.mongodb_adapter.write(
                [MatrixWrapper()], "analytics_correlation_matrix"
            )

            logger.info(
                f"Correlation calculated for {len(results)} symbols "
                f"against benchmark {benchmark}"
            )

            return results

        except Exception as e:
            logger.error(f"Error calculating correlation: {e}", exc_info=True)
            return {}
