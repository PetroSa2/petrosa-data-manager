"""
Statistical anomaly detection without ML dependencies.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import AuditRepository, CandleRepository

logger = logging.getLogger(__name__)


class StatisticalAnomalyDetector:
    """
    Statistical anomaly detection using traditional methods.

    Uses Z-score, MAD, and moving average deviations.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize statistical anomaly detector.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.audit_repo = AuditRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def detect_anomalies(
        self,
        symbol: str,
        timeframe: str,
        method: str = "zscore",
        threshold: float = 3.0,
    ) -> list:
        """
        Detect anomalies using statistical methods.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            method: Detection method ('zscore', 'mad', 'moving_avg')
            threshold: Threshold for anomaly detection

        Returns:
            List of anomaly dictionaries
        """
        try:
            # Fetch recent candles
            end = datetime.utcnow()
            start = end - timedelta(days=7)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 30:
                logger.warning(
                    f"Insufficient data for anomaly detection: {len(candles)}"
                )
                return []

            # Convert to DataFrame
            df = pd.DataFrame(candles)
            df["close"] = df["close"].apply(
                lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
            )

            # Detect anomalies based on method
            if method == "zscore":
                anomaly_mask = self._detect_zscore_anomalies(df["close"], threshold)
            elif method == "mad":
                anomaly_mask = self._detect_mad_anomalies(df["close"], threshold)
            elif method == "moving_avg":
                anomaly_mask = self._detect_moving_avg_anomalies(
                    df["close"], window=20, threshold=threshold
                )
            else:
                logger.error(f"Unknown detection method: {method}")
                return []

            # Extract anomalies
            anomalies = []
            anomaly_indices = np.where(anomaly_mask)[0]

            for idx in anomaly_indices:
                anomaly = {
                    "timestamp": df.iloc[idx]["timestamp"],
                    "price": df.iloc[idx]["close"],
                    "method": method,
                    "severity": self._calculate_severity(df["close"], idx),
                    "reason": "statistical_outlier",
                }
                anomalies.append(anomaly)

                # Log to audit repository
                await self._log_anomaly(symbol, timeframe, anomaly)

            logger.info(
                f"Detected {len(anomalies)} anomalies for {symbol} {timeframe} using {method}"
            )
            return anomalies

        except Exception as e:
            logger.error(f"Error detecting anomalies: {e}", exc_info=True)
            return []

    def _detect_zscore_anomalies(
        self, data: pd.Series, threshold: float = 3.0
    ) -> np.ndarray:
        """
        Detect anomalies using Z-score method.

        Args:
            data: Price series
            threshold: Z-score threshold

        Returns:
            Boolean array indicating anomalies
        """
        mean = data.mean()
        std = data.std()
        z_scores = np.abs((data - mean) / std)
        return z_scores > threshold

    def _detect_mad_anomalies(
        self, data: pd.Series, threshold: float = 3.5
    ) -> np.ndarray:
        """
        Detect anomalies using MAD (Median Absolute Deviation).

        More robust to outliers than Z-score.

        Args:
            data: Price series
            threshold: MAD threshold

        Returns:
            Boolean array indicating anomalies
        """
        median = data.median()
        mad = np.median(np.abs(data - median))

        if mad == 0:
            return np.zeros(len(data), dtype=bool)

        modified_z_scores = 0.6745 * (data - median) / mad
        return np.abs(modified_z_scores) > threshold

    def _detect_moving_avg_anomalies(
        self, data: pd.Series, window: int = 20, threshold: float = 2.0
    ) -> np.ndarray:
        """
        Detect anomalies as deviations from moving average.

        Args:
            data: Price series
            window: Rolling window size
            threshold: Number of standard deviations

        Returns:
            Boolean array indicating anomalies
        """
        ma = data.rolling(window=window).mean()
        std = data.rolling(window=window).std()
        deviation = np.abs(data - ma) / std
        return deviation > threshold

    def _calculate_severity(self, data: pd.Series, idx: int) -> str:
        """
        Calculate anomaly severity.

        Args:
            data: Price series
            idx: Index of anomaly

        Returns:
            Severity level ('low', 'medium', 'high', 'critical')
        """
        mean = data.mean()
        std = data.std()
        value = data.iloc[idx]
        z_score = abs((value - mean) / std) if std > 0 else 0

        if z_score > 5:
            return "critical"
        elif z_score > 4:
            return "high"
        elif z_score > 3:
            return "medium"
        else:
            return "low"

    async def _log_anomaly(self, symbol: str, timeframe: str, anomaly: dict) -> None:
        """Log detected anomaly to audit logs."""
        try:
            dataset_id = f"candles_{symbol}_{timeframe}"
            details = (
                f"Anomaly detected at {anomaly['timestamp']}: "
                f"price={anomaly['price']:.2f}, method={anomaly['method']}"
            )
            await self.audit_repo.log_health_check(
                dataset_id=dataset_id,
                symbol=symbol,
                details=details,
                severity=anomaly.get("severity", "medium"),
            )
        except Exception as e:
            logger.error(f"Failed to log anomaly: {e}")
