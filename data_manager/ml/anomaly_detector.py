"""
ML-based anomaly detection using sklearn.

Optional module - requires scikit-learn.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

try:
    from sklearn.ensemble import IsolationForest

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("scikit-learn not available, ML anomaly detection disabled")

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import AuditRepository, CandleRepository

logger = logging.getLogger(__name__)


class MLAnomalyDetector:
    """
    ML-based anomaly detection using Isolation Forest.

    Requires scikit-learn to be installed.
    """

    def __init__(self, db_manager: DatabaseManager, contamination: float = 0.05):
        """
        Initialize ML anomaly detector.

        Args:
            db_manager: Database manager instance
            contamination: Expected proportion of outliers (default: 5%)
        """
        if not SKLEARN_AVAILABLE:
            raise ImportError(
                "scikit-learn is required for ML anomaly detection. "
                "Install with: pip install scikit-learn"
            )

        self.db_manager = db_manager
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.audit_repo = AuditRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

        # Initialize Isolation Forest model
        self.model = IsolationForest(
            contamination=contamination, random_state=42, n_estimators=100
        )

    async def detect_price_anomalies(
        self, symbol: str, timeframe: str, window_days: int = 7
    ) -> list:
        """
        Detect price anomalies using Isolation Forest.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1h', '1d')
            window_days: Days of history to analyze

        Returns:
            List of detected anomalies
        """
        try:
            # Fetch recent candles
            end = datetime.utcnow()
            start = end - timedelta(days=window_days)
            candles = await self.candle_repo.get_range(symbol, timeframe, start, end)

            if len(candles) < 50:
                logger.warning(f"Insufficient data for ML detection: {len(candles)}")
                return []

            # Convert to DataFrame
            df = pd.DataFrame(candles)

            # Convert Decimal to float
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].apply(
                    lambda x: float(x) if isinstance(x, Decimal) else float(str(x))
                )

            # Feature engineering
            df["price_change"] = df["close"].pct_change()
            df["volume_change"] = df["volume"].pct_change()
            df["volatility"] = df["close"].rolling(window=20).std()
            df["high_low_ratio"] = (df["high"] - df["low"]) / df["low"]

            # Create feature matrix
            features = df[
                [
                    "close",
                    "volume",
                    "volatility",
                    "price_change",
                    "volume_change",
                    "high_low_ratio",
                ]
            ].dropna()

            if len(features) < 30:
                logger.warning("Insufficient features after processing")
                return []

            # Train model on all data
            self.model.fit(features)

            # Predict anomalies (-1 = anomaly, 1 = normal)
            predictions = self.model.predict(features)

            # Extract anomaly indices
            anomaly_indices = np.where(predictions == -1)[0]

            # Create anomaly records
            anomalies = []
            for idx in anomaly_indices:
                original_idx = features.index[idx]
                anomaly = {
                    "timestamp": df.iloc[original_idx]["timestamp"],
                    "price": df.iloc[original_idx]["close"],
                    "volume": df.iloc[original_idx]["volume"],
                    "method": "isolation_forest",
                    "severity": "medium",
                    "reason": "ml_outlier",
                    "features": {
                        "price_change": features.iloc[idx]["price_change"],
                        "volume_change": features.iloc[idx]["volume_change"],
                        "volatility": features.iloc[idx]["volatility"],
                    },
                }
                anomalies.append(anomaly)

                # Log to audit repository
                await self._log_anomaly(symbol, timeframe, anomaly)

            logger.info(
                f"ML detected {len(anomalies)} anomalies for {symbol} {timeframe}"
            )
            return anomalies

        except Exception as e:
            logger.error(f"Error in ML anomaly detection: {e}", exc_info=True)
            return []

    async def _log_anomaly(self, symbol: str, timeframe: str, anomaly: dict) -> None:
        """Log detected anomaly to audit logs."""
        try:
            dataset_id = f"candles_{symbol}_{timeframe}"
            details = (
                f"ML Anomaly at {anomaly['timestamp']}: "
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
