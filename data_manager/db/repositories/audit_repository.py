"""
Repository for audit log operations.
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

logger = logging.getLogger(__name__)


class AuditRepository(BaseRepository):
    """Repository for operational audit logs in MongoDB."""

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
        if self.mongodb is None:
            return await self._legacy_write(
                audit_log=None,
                kind="gap",
                dataset_id=dataset_id,
                symbol=symbol,
                gap_start=gap_start,
                gap_end=gap_end,
                severity=severity,
            )

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

            await self.mongodb.write([AuditLog()], "audit_logs")
            self._schedule_mysql_copy(audit_log)
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
        if self.mongodb is None:
            return await self._legacy_write(
                audit_log=None,
                kind="health_check",
                dataset_id=dataset_id,
                symbol=symbol,
                details=details,
                severity=severity,
            )

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

            await self.mongodb.write([AuditLog()], "audit_logs")
            self._schedule_mysql_copy(audit_log)
            return True

        except Exception as e:
            logger.error(f"Failed to log health check: {e}")
            return False

    async def get_recent_logs(
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
        if self.mongodb is None:
            return await self._legacy_recent(dataset_id, limit)

        try:
            logs = await self.mongodb.find_filtered(
                "audit_logs",
                filters={"dataset_id": dataset_id},
                limit=limit,
                sort_field="timestamp",
                sort_order=-1,
            )
            return logs
        except Exception as e:
            logger.error(f"Failed to get recent logs: {e}")
            return []

    async def _legacy_write(
        self, audit_log, kind, dataset_id, symbol, severity, **kwargs
    ):
        if self.mysql is None:
            logger.warning("audit_logs_mongodb_unavailable")
            return False
        try:
            record = audit_log or {
                "audit_id": str(uuid.uuid4()),
                "dataset_id": dataset_id,
                "symbol": symbol,
                "audit_type": kind,
                "severity": severity,
                "details": (
                    f"Gap from {kwargs['gap_start']} to {kwargs['gap_end']}"
                    if kind == "gap"
                    else kwargs["details"]
                ),
                "timestamp": datetime.now(UTC),
            }

            class AuditLog:
                def model_dump(self):
                    return record

            await asyncio.to_thread(self.mysql.write, [AuditLog()], "audit_logs")
            return True
        except Exception as exc:
            logger.error("Failed to write legacy audit log: %s", exc)
            return False

    async def _legacy_recent(self, dataset_id: str | None, limit: int) -> list[dict]:
        if self.mysql is None:
            logger.warning("audit_logs_mongodb_unavailable")
            return []
        try:
            return await asyncio.to_thread(
                self.mysql.query_latest, "audit_logs", symbol=dataset_id, limit=limit
            )
        except Exception as exc:
            logger.error("Failed to read legacy audit logs: %s", exc)
            return []

    def _schedule_mysql_copy(self, record: dict) -> None:
        if not constants.MONITORING_MYSQL_COPY_ENABLED or self.mysql is None:
            return

        async def copy() -> None:
            try:

                class AuditLog:
                    def model_dump(self):
                        return record

                await asyncio.to_thread(self.mysql.write, [AuditLog()], "audit_logs")
            except Exception as exc:
                logger.warning("audit_logs_mysql_copy_failed: %s", exc)

        asyncio.create_task(copy())

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
