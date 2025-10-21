"""
Repository for catalog operations.
"""

import logging

from data_manager.db.repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class CatalogRepository(BaseRepository):
    """Repository for managing catalog in MySQL."""

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

            self.mysql.write([Dataset()], "datasets")
            return True
        except Exception as e:
            logger.error(f"Failed to upsert dataset: {e}")
            return False

    def get_all_datasets(self) -> list[dict]:
        """
        Get all datasets.

        Returns:
            List of dataset dictionaries
        """
        try:
            # Query all datasets (large limit)
            return self.mysql.query_latest("datasets", limit=10000)
        except Exception as e:
            logger.error(f"Failed to get all datasets: {e}")
            return []

    def get_dataset(self, dataset_id: str) -> dict | None:
        """
        Get dataset by ID.

        Args:
            dataset_id: Dataset identifier

        Returns:
            Dataset dictionary or None
        """
        try:
            datasets = self.get_all_datasets()
            for dataset in datasets:
                if dataset.get("dataset_id") == dataset_id:
                    return dataset
            return None
        except Exception as e:
            logger.error(f"Failed to get dataset: {e}")
            return None
