"""
Repository for audit log operations.
"""

import logging
import uuid
from datetime import datetime

from data_manager.db.repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class AuditRepository(BaseRepository):
    """Repository for managing audit logs in MySQL."""

    async def log_gap(
        self,
        dataset_id: str,
        symbol: str,
        gap_start: datetime,
        gap_end: datetime,
        severity: str = "medium",
    ) -> bool:
        """
        Log a data gap.

        Args:
            dataset_id: Dataset identifier
            symbol: Trading pair symbol
            gap_start: Gap start timestamp
            gap_end: Gap end timestamp
            severity: Severity level

        Returns:
            True if successful
        """
        try:
            audit_log = {
                "audit_id": str(uuid.uuid4()),
                "dataset_id": dataset_id,
                "symbol": symbol,
                "audit_type": "gap",
                "severity": severity,
                "details": f"Gap from {gap_start} to {gap_end}",
                "timestamp": datetime.utcnow(),
            }

            # Create a simple Pydantic-like object
            class AuditLog:
                def model_dump(self):
                    return audit_log

            self.mysql.write([AuditLog()], "audit_logs")
            return True

        except Exception as e:
            logger.error(f"Failed to log gap: {e}")
            return False

    async def log_health_check(
        self, dataset_id: str, symbol: str, details: str, severity: str = "info"
    ) -> bool:
        """
        Log a health check result.

        Args:
            dataset_id: Dataset identifier
            symbol: Trading pair symbol
            details: Check details
            severity: Severity level

        Returns:
            True if successful
        """
        try:
            audit_log = {
                "audit_id": str(uuid.uuid4()),
                "dataset_id": dataset_id,
                "symbol": symbol,
                "audit_type": "health_check",
                "severity": severity,
                "details": details,
                "timestamp": datetime.utcnow(),
            }

            class AuditLog:
                def model_dump(self):
                    return audit_log

            self.mysql.write([AuditLog()], "audit_logs")
            return True

        except Exception as e:
            logger.error(f"Failed to log health check: {e}")
            return False

    def get_recent_logs(
        self, dataset_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        """
        Get recent audit logs.

        Args:
            dataset_id: Optional dataset filter
            limit: Maximum number of logs

        Returns:
            List of audit log dictionaries
        """
        try:
            logs = self.mysql.query_latest("audit_logs", symbol=dataset_id, limit=limit)
            return logs
        except Exception as e:
            logger.error(f"Failed to get recent logs: {e}")
            return []
