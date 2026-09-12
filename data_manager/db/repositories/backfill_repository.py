"""
Repository for backfill job operations.
"""

import logging
from datetime import datetime

from sqlalchemy import and_, func
from sqlalchemy.sql import select

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
            table = self.mysql._get_table("backfill_jobs")
            engine = self.mysql._ensure_connected()
            stmt = select(table).where(table.c.job_id == job_id).limit(1)
            with engine.connect() as conn:
                row = conn.execute(stmt).fetchone()
                return dict(row._mapping) if row else None
        except Exception as e:
            logger.error(f"Failed to get backfill job: {e}")
            return None

    def list_jobs(
        self,
        status: str | None = None,
        symbol: str | None = None,
        data_type: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """
        List backfill jobs with filtering, sorting, and pagination.

        Args:
            status: Filter by job status
            symbol: Filter by trading symbol
            data_type: Filter by data type
            from_time: Filter jobs created at/after this time
            to_time: Filter jobs created before this time
            sort_by: Column to sort by (falls back to created_at if unknown)
            sort_order: "asc" or "desc"
            limit: Maximum number of rows to return
            offset: Pagination offset

        Returns:
            Tuple of (jobs, total_count matching filters before pagination)
        """
        try:
            table = self.mysql._get_table("backfill_jobs")
            engine = self.mysql._ensure_connected()

            conditions = []
            if status:
                conditions.append(table.c.status == status)
            if symbol:
                conditions.append(table.c.symbol == symbol)
            if data_type:
                conditions.append(table.c.data_type == data_type)
            if from_time:
                conditions.append(table.c.created_at >= from_time)
            if to_time:
                conditions.append(table.c.created_at < to_time)

            count_stmt = select(func.count()).select_from(table)
            if conditions:
                count_stmt = count_stmt.where(and_(*conditions))

            sort_column = getattr(table.c, sort_by, None)
            if sort_column is None:
                sort_column = table.c.created_at
            order = sort_column.desc() if sort_order == "desc" else sort_column.asc()

            stmt = select(table)
            if conditions:
                stmt = stmt.where(and_(*conditions))
            stmt = stmt.order_by(order).limit(limit).offset(offset)

            with engine.connect() as conn:
                total = conn.execute(count_stmt).scalar() or 0
                rows = conn.execute(stmt).fetchall()
                jobs = [dict(row._mapping) for row in rows]
                return jobs, total
        except Exception as e:
            logger.error(f"Failed to list backfill jobs: {e}")
            return [], 0

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
