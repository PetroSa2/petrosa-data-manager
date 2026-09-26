"""
Repository for health metrics operations.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

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
        if self.mysql is None:
            logger.warning("health_metrics_mysql_unavailable")
            return False

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
                "timestamp": datetime.now(UTC),
            }

            class HealthMetric:
                def model_dump(self):
                    return health_record

            # petrosa-data-manager#312: self.mysql.write() is a synchronous
            # SQLAlchemy call blocking on network I/O; called inline from an
            # `async def` it stalls the whole event loop (including the
            # liveness/readiness handlers) for the duration of the DB
            # round-trip. Offload to a worker thread.
            await asyncio.to_thread(
                self.mysql.write, [HealthMetric()], "health_metrics"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to insert health metrics: {e}")
            return False

    async def get_latest_health(self, dataset_id: str, symbol: str) -> dict | None:
        """
        Get latest health metrics for dataset.

        Args:
            dataset_id: Dataset identifier
            symbol: Trading pair symbol

        Returns:
            Health metrics dictionary or None
        """
        if self.mysql is None:
            logger.warning("health_metrics_mysql_unavailable")
            return None

        try:
            # Query latest by dataset_id (using symbol field as filter).
            # petrosa-data-manager#312: see insert() comment above.
            results = await asyncio.to_thread(
                self.mysql.query_latest, "health_metrics", symbol=symbol, limit=1
            )
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Failed to get latest health: {e}")
            return None
