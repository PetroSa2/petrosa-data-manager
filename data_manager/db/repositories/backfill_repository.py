"""
Repository for backfill job operations.
"""

import logging

from data_manager.db.repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class BackfillRepository(BaseRepository):
    """Repository for managing backfill jobs in MySQL."""

    async def create_job(self, job: dict) -> bool:
        """
        Create a new backfill job.

        Args:
            job: Job dictionary with all fields

        Returns:
            True if successful
        """
        try:

            class Job:
                def model_dump(self):
                    return job

            self.mysql.write([Job()], "backfill_jobs")
            return True
        except Exception as e:
            logger.error(f"Failed to create backfill job: {e}")
            return False

    def get_job(self, job_id: str) -> dict | None:
        """
        Get backfill job by ID.

        Args:
            job_id: Job identifier

        Returns:
            Job dictionary or None
        """
        try:
            # For now, use query_latest as a workaround
            # TODO: Implement proper query by ID
            jobs = self.mysql.query_latest("backfill_jobs", limit=1000)
            for job in jobs:
                if job.get("job_id") == job_id:
                    return job
            return None
        except Exception as e:
            logger.error(f"Failed to get backfill job: {e}")
            return None

    async def update_status(
        self, job_id: str, status: str, error: str | None = None
    ) -> bool:
        """
        Update job status.

        Args:
            job_id: Job identifier
            status: New status
            error: Optional error message

        Returns:
            True if successful
        """
        try:
            # TODO: Implement proper update logic
            logger.info(f"Updating job {job_id} to status {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update job status: {e}")
            return False
