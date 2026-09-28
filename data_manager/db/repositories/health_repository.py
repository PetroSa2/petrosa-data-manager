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

import constants
from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.health import DataHealthMetrics

logger = logging.getLogger(__name__)


class HealthRepository(BaseRepository):
    """Repository for operational health metrics in MongoDB."""

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
        if self.mongodb is None:
            return await self._legacy_mysql_insert(
                health_record=None,
                metrics=metrics,
                dataset_id=dataset_id,
                symbol=symbol,
            )

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

            await self.mongodb.write([HealthMetric()], "health_metrics")
            self._schedule_mysql_copy(health_record)
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
        if self.mongodb is None:
            return await self._legacy_mysql_latest(symbol)

        try:
            results = await self.mongodb.query_latest(
                "health_metrics", symbol=symbol, limit=1
            )
            return results[0] if results else None
        except TypeError:
            return await self._legacy_mysql_latest(symbol)
        except Exception as e:
            logger.error(f"Failed to get latest health: {e}")
            return None

    def _schedule_mysql_copy(self, record: dict) -> None:
        if not constants.MONITORING_MYSQL_COPY_ENABLED or self.mysql is None:
            return

        async def copy() -> None:
            try:

                class HealthMetric:
                    def model_dump(self):
                        return record

                await asyncio.to_thread(
                    self.mysql.write, [HealthMetric()], "health_metrics"
                )
            except Exception as exc:
                logger.warning("health_metrics_mysql_copy_failed: %s", exc)

        asyncio.create_task(copy())

    async def _legacy_mysql_insert(
        self, health_record, metrics, dataset_id, symbol
    ) -> bool:
        if self.mysql is None:
            logger.warning("health_metrics_mongodb_unavailable")
            return False
        try:
            record = health_record or {
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
                    return record

            await asyncio.to_thread(
                self.mysql.write, [HealthMetric()], "health_metrics"
            )
            return True
        except Exception as exc:
            logger.error("Failed to insert legacy health metrics: %s", exc)
            return False

    async def _legacy_mysql_latest(self, symbol: str) -> dict | None:
        if self.mysql is None:
            logger.warning("health_metrics_mongodb_unavailable")
            return None
        try:
            results = await asyncio.to_thread(
                self.mysql.query_latest, "health_metrics", symbol=symbol, limit=1
            )
            return results[0] if results else None
        except Exception as exc:
            logger.error("Failed to get legacy health metrics: %s", exc)
            return None
