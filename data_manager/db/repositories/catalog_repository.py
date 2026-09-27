"""
Repository for catalog operations.
"""

import asyncio
import inspect
import logging

from data_manager.db.repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class CatalogRepository(BaseRepository):
    """Repository for catalog metadata in MongoDB."""

    async def upsert_dataset(self, dataset: dict) -> bool:
        """
        Insert or update a dataset.

        Args:
            dataset: Dataset dictionary

        Returns:
            True if successful
        """
        try:

            class Dataset:
                def model_dump(self):
                    return dataset

            if self.mongodb is None:
                return await self._legacy_upsert(dataset)
            await self.mongodb.upsert_one(
                "datasets", {"dataset_id": dataset["dataset_id"]}, dataset
            )
            return True
        except Exception as e:
            logger.error(f"Failed to upsert dataset: {e}")
            return False

    def get_all_datasets(self):
        """
        Get all datasets.

        Returns:
            List of dataset dictionaries
        """
        if self.mongodb is not None and inspect.iscoroutinefunction(
            self.mongodb.find_filtered
        ):

            async def read_mongo():
                try:
                    return await self.mongodb.find_filtered(
                        "datasets", limit=10000, sort_field="updated_at", sort_order=-1
                    )
                except Exception as exc:
                    logger.error("Failed to get all datasets: %s", exc)
                    return []

            return read_mongo()
        try:
            return (
                self.mysql.query_latest("datasets", limit=10000) if self.mysql else []
            )
        except Exception as exc:
            logger.error("Failed to get all datasets: %s", exc)
            return []

    async def get_all_datasets_async(self) -> list[dict]:
        """Read catalog data without running the legacy MySQL call on the loop."""
        if self.mongodb is not None and inspect.iscoroutinefunction(
            self.mongodb.find_filtered
        ):
            return await self.mongodb.find_filtered(
                "datasets", limit=10000, sort_field="updated_at", sort_order=-1
            )
        return await asyncio.to_thread(self._get_all_datasets_sync)

    def _get_all_datasets_sync(self) -> list[dict]:
        try:
            return (
                self.mysql.query_latest("datasets", limit=10000) if self.mysql else []
            )
        except Exception as exc:
            logger.error("Failed to get all datasets: %s", exc)
            return []

    def get_dataset(self, dataset_id: str):
        """
        Get dataset by ID.

        Args:
            dataset_id: Dataset identifier

        Returns:
            Dataset dictionary or None
        """
        if self.mongodb is not None and inspect.iscoroutinefunction(
            self.mongodb.find_filtered
        ):

            async def read_mongo():
                try:
                    datasets = await self.mongodb.find_filtered(
                        "datasets", filters={"dataset_id": dataset_id}, limit=1
                    )
                    return datasets[0] if datasets else None
                except Exception as exc:
                    logger.error("Failed to get dataset: %s", exc)
                    return None

            return read_mongo()
        return self._legacy_get(dataset_id)

    async def get_dataset_async(self, dataset_id: str) -> dict | None:
        """Read one catalog entry without blocking the event loop."""
        if self.mongodb is not None and inspect.iscoroutinefunction(
            self.mongodb.find_filtered
        ):
            datasets = await self.mongodb.find_filtered(
                "datasets", filters={"dataset_id": dataset_id}, limit=1
            )
            return datasets[0] if datasets else None
        return await asyncio.to_thread(self._legacy_get, dataset_id)

    async def _legacy_upsert(self, dataset: dict) -> bool:
        if self.mysql is None:
            return False
        try:

            class Dataset:
                def model_dump(self):
                    return dataset

            await asyncio.to_thread(self.mysql.write, [Dataset()], "datasets")
            return True
        except Exception as exc:
            logger.error("Failed to upsert legacy dataset: %s", exc)
            return False

    def _legacy_get(self, dataset_id: str) -> dict | None:
        try:
            datasets = self.get_all_datasets()
        except Exception as exc:
            logger.error("Failed to get dataset: %s", exc)
            return None
        return next((d for d in datasets if d.get("dataset_id") == dataset_id), None)
