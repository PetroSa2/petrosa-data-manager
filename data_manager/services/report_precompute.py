"""Leader-only cache for the CIO reports used on the decision path."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from prometheus_client import Gauge, Histogram

import constants

logger = logging.getLogger(__name__)
REPORT_CACHE = "report_cache"
_refreshing: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "report_refreshing", default=False
)
report_age_seconds = Gauge(
    "data_manager_report_age_seconds",
    "Age of the latest precomputed report",
    ["report"],
)
report_stage_seconds = Histogram(
    "data_manager_report_stage_seconds",
    "Report refresh stage duration",
    ["report", "stage"],
)


def refreshing() -> bool:
    return _refreshing.get()


def cache_key(report: str, **params: Any) -> str:
    suffix = ":".join(f"{key}={params[key]}" for key in sorted(params))
    return report if not suffix else f"{report}:{suffix}"


class ReportPrecomputer:
    """Refreshes reports in MongoDB and serves their last good values."""

    def __init__(self, db_manager: Any, leader_election: Any = None) -> None:
        self.db_manager = db_manager
        self.leader_election = leader_election
        self.task: asyncio.Task[None] | None = None
        self.running = False

    @property
    def collection(self) -> Any:
        return self.db_manager.mongodb_adapter.db[REPORT_CACHE]

    async def start(self) -> None:
        if not constants.ENABLE_REPORT_PRECOMPUTE or self.task:
            return
        await self.collection.create_index(
            "computed_at", expireAfterSeconds=constants.REPORT_CACHE_TTL_SECONDS
        )
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def get(self, report: str, **params: Any) -> dict[str, Any] | None:
        row = await self.collection.find_one({"_id": cache_key(report, **params)})
        if not row:
            return None
        body = dict(row.get("body", {}))
        computed_at = row.get("computed_at")
        if isinstance(computed_at, datetime):
            age = max(0.0, (datetime.now(UTC) - computed_at).total_seconds())
            report_age_seconds.labels(report=report).set(age)
            metadata = body.setdefault("metadata", {})
            metadata["calculated_at"] = computed_at.isoformat()
            metadata["computed_at"] = computed_at.isoformat()
            metadata["age_seconds"] = age
            metadata["stale"] = age > self._interval(report) * 3
        return body

    async def _put(self, report: str, body: dict[str, Any], **params: Any) -> None:
        computed_at = datetime.now(UTC)
        body = dict(body)
        metadata = body.setdefault("metadata", {})
        metadata.update(
            calculated_at=computed_at.isoformat(),
            computed_at=computed_at.isoformat(),
            age_seconds=0.0,
            stale=False,
        )
        if report == "risk_inputs":
            body["as_of"] = computed_at.isoformat()
        await self.collection.replace_one(
            {"_id": cache_key(report, **params)},
            {
                "_id": cache_key(report, **params),
                "body": body,
                "computed_at": computed_at,
            },
            upsert=True,
        )
        report_age_seconds.labels(report=report).set(0)

    def _interval(self, report: str) -> int:
        return (
            constants.REPORT_RISK_INTERVAL_SECONDS
            if report == "risk_inputs"
            else constants.REPORT_SLIPPAGE_INTERVAL_SECONDS
        )

    async def _refresh(
        self, report: str, callback: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        token = _refreshing.set(True)
        started = monotonic()
        try:
            body = await callback()
            await self._put(
                report, body, window_days=30 if report != "risk_inputs" else 30
            )
            report_stage_seconds.labels(report=report, stage="compute").observe(
                monotonic() - started
            )
        except Exception:
            logger.exception("report refresh failed", extra={"report": report})
        finally:
            _refreshing.reset(token)

    async def _run(self) -> None:
        from data_manager.api.routes import analysis, risk

        next_slippage = 0.0
        next_risk = 0.0
        try:
            while self.running:
                if (
                    self.leader_election is not None
                    and not self.leader_election.is_leader
                ):
                    await asyncio.sleep(1)
                    continue
                now = monotonic()
                if now >= next_slippage:
                    await self._refresh(
                        "slippage_by_regime",
                        lambda: analysis.get_slippage_by_regime(window_days=30),
                    )
                    await self._refresh(
                        "rounds", lambda: analysis.get_closed_rounds(window_days=30)
                    )
                    next_slippage = now + constants.REPORT_SLIPPAGE_INTERVAL_SECONDS
                if now >= next_risk:
                    await self._refresh(
                        "risk_inputs", lambda: risk.get_risk_inputs(window_days=30)
                    )
                    next_risk = now + constants.REPORT_RISK_INTERVAL_SECONDS
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return
