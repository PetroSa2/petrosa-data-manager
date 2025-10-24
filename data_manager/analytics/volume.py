"""
Volume metrics calculator.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import CandleRepository
from data_manager.models.analytics import MetricMetadata, VolumeMetrics

logger = logging.getLogger(__name__)


class VolumeCalculator:
    """Calculates volume metrics from candle data."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize volume calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_volume(
        self,
        symbol: str,
        timeframe: str,
        window_hours: int = 24,
    ) -> VolumeMetrics | None:
        """
        Calculate volume metrics for a symbol.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            window_hours: Window size in hours

        Returns:
            VolumeMetrics or None if insufficient data
        """
        try:
            # Fetch candles from MongoDB
            end = datetime.utcnow()
            start = end - timedelta(hours=window_hours)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 5:  # Need minimum data points
                logger.warning(
                    f"Insufficient data for volume calculation: {len(candles)} candles"
                )
                return None

            # Convert to DataFrame
            df = pd.DataFrame(candles)

            # Convert Decimal to float
            df["volume"] = df["volume"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )

            # Total volume
            total_volume = df["volume"].sum()

            # Volume moving averages
            sma_window = min(20, len(df))
            volume_sma = df["volume"].rolling(window=sma_window).mean().iloc[-1]
            volume_ema = df["volume"].ewm(span=sma_window).mean().iloc[-1]

            # Volume delta (approximation - would need buy/sell data for accuracy)
            volume_delta = 0.0  # TODO: Calculate from trade data

            # Volume spike ratio (current vs baseline)
            current_volume = df["volume"].iloc[-1]
            baseline_volume = df["volume"].median()
            volume_spike_ratio = (
                current_volume / baseline_volume if baseline_volume > 0 else 1.0
            )

            # Create metadata
            metadata = MetricMetadata(
                method="aggregation",
                window=f"{window_hours}h",
                parameters={"sma_window": sma_window},
                completeness=100.0,  # Assume complete for now
                computed_at=datetime.utcnow(),
            )

            # Create metrics object
            metrics = VolumeMetrics(
                symbol=symbol,
                timeframe=timeframe,
                total_volume=Decimal(str(total_volume)),
                volume_sma=Decimal(str(volume_sma)),
                volume_ema=Decimal(str(volume_ema)),
                volume_delta=Decimal(str(volume_delta)),
                volume_spike_ratio=Decimal(str(volume_spike_ratio)),
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_volume"
            await self.db_manager.mongodb_adapter.write([metrics], collection)

            logger.info(
                f"Volume calculated for {symbol} {timeframe}: "
                f"total={total_volume:.2f}, sma={volume_sma:.2f}"
            )

            return metrics

        except Exception as e:
            logger.error(
                f"Error calculating volume for {symbol} {timeframe}: {e}",
                exc_info=True,
            )
            return None
