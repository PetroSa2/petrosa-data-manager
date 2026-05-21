"""
Repository for audit log operations.
"""

import logging
import uuid
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

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
                "timestamp": datetime.now(UTC),
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
                "timestamp": datetime.now(UTC),
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

    async def query_decisions(
        self,
        *,
        strategy_id: str | None = None,
        action: str | None = None,
        decision_id: str | None = None,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Cross-service audit-trail query against the ``cio_decisions`` collection.

        Per ticket #605 P4.5, callers (operators, dashboard, lifecycle
        reader) need to slice the CIO decision audit-trail by
        ``strategy_id``, CIO action category (ADMIT / EXECUTE / VETO /
        …), and/or ``decision_id`` for cross-service joins back to
        execution events and P&L. Time-window is composable on top.

        The decisions live in MongoDB ``cio_decisions`` (persisted by
        ``DecisionConsumer``) — indexes on every filter field already
        exist (see ``MongoDBAdapter.ensure_indexes`` for the
        ``cio_decisions`` block), so the query is index-served regardless
        of which filter combination the caller picks.

        Args:
            strategy_id: filter by the strategy that produced the decision
            action: filter by CIO action verb (matches stored payload — the
                caller is responsible for casing; this matches the
                producer convention in petrosa-cio where the wire-level
                value is the lower-cased ``ActionType`` value)
            decision_id: exact match — uniquely identifies one decision
            symbol: filter by trading pair symbol
            start: inclusive lower-bound on ``timestamp``
            end: exclusive upper-bound on ``timestamp``
            limit: hard cap on returned records (caller-side clamped)

        Returns:
            list of decision-event dicts, newest first, with the Mongo
            ``_id`` stripped. Empty list on adapter failure (caller logs
            already) so the HTTP layer can serve 200 with `[]` rather
            than crashing on Mongo hiccups.
        """
        if self.mongodb is None:
            logger.warning(
                "audit_query_decisions_no_mongo",
                extra={"strategy_id": strategy_id, "action": action},
            )
            return []
        try:
            return await self.mongodb.find_filtered(
                "cio_decisions",
                filters={
                    "strategy_id": strategy_id,
                    "action": action,
                    "decision_id": decision_id,
                    "symbol": symbol,
                },
                start=start,
                end=end,
                limit=limit,
                sort_field="timestamp",
                sort_order=-1,
            )
        except Exception as e:
            logger.error(
                "audit_query_decisions_failed",
                extra={"error": str(e)},
            )
            return []
