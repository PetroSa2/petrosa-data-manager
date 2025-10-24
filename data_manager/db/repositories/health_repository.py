"""
Repository for health metrics operations.
"""

import logging
import uuid
from datetime import datetime

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.health import DataHealthMetrics

logger = logging.getLogger(__name__)


class HealthRepository(BaseRepository):
    """Repository for managing health metrics in MySQL."""

    async def insert(
        self, dataset_id: str, symbol: str, metrics: DataHealthMetrics
    ) -> bool:
        """
        Insert health metrics.

        Args:
            dataset_id: Dataset identifier
            symbol: Trading pair symbol
            metrics: DataHealthMetrics instance

        Returns:
            True if successful
        """
        try:
            health_record = {
                "metric_id": str(uuid.uuid4()),
                "dataset_id": dataset_id,
                "symbol": symbol,
                "completeness": float(metrics.completeness),
                "freshness_seconds": metrics.freshness_seconds,
                "gaps_count": metrics.gaps_count,
                "duplicates_count": metrics.duplicates_count,
                "quality_score": float(metrics.quality_score),
                "timestamp": datetime.utcnow(),
            }

            class HealthMetric:
                def model_dump(self):
                    return health_record

            self.mysql.write([HealthMetric()], "health_metrics")
            return True

        except Exception as e:
            logger.error(f"Failed to insert health metrics: {e}")
            return False

    def get_latest_health(self, dataset_id: str, symbol: str) -> dict | None:
        """
        Get latest health metrics for dataset.

        Args:
            dataset_id: Dataset identifier
            symbol: Trading pair symbol

        Returns:
            Health metrics dictionary or None
        """
        try:
            # Query latest by dataset_id (using symbol field as filter)
            results = self.mysql.query_latest("health_metrics", symbol=symbol, limit=1)
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Failed to get latest health: {e}")
            return None
