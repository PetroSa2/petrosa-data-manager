"""Periodic, leader-only ledger tie-out metrics refresh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from prometheus_client import Counter

import constants
from data_manager.api.routes.ledger import (
    OPEN_POSITION_VARIANCE,
    TIEOUT_CUMULATIVE,
    TIEOUT_LAST_COMPLETE_DAY,
    TIEOUT_VARIANCE,
)

logger = logging.getLogger(__name__)

ERRORS_TOTAL = Counter(
    "ledger_tieout_scheduler_errors_total",
    "Ledger tie-out scheduler cycles that failed",
)


class LedgerTieoutScheduler:
    """Run read-only ledger checks on the elected leader replica."""

    def __init__(
        self,
        repository: Any,
        leader_election: Any = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.leader_election = leader_election
        self.interval_seconds = max(1, constants.LEDGER_TIEOUT_INTERVAL_SECONDS)
        self.lookback_days = max(0, constants.LEDGER_TIEOUT_LOOKBACK_DAYS)
        self.enabled = constants.LEDGER_TIEOUT_SCHEDULER_ENABLED
        self._now = now or (lambda: datetime.now(UTC))
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "ledger_tieout_scheduler_started interval_seconds=%s lookback_days=%s",
            self.interval_seconds,
            self.lookback_days,
        )

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _is_leader(self) -> bool:
        return self.leader_election is None or self.leader_election.is_leader

    async def _loop(self) -> None:
        while self.running:
            try:
                await self.run_cycle()
            except Exception as exc:  # noqa: BLE001
                ERRORS_TOTAL.inc()
                logger.error(
                    "ledger_tieout_scheduler_cycle_failed error_type=%s error=%s",
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
            await asyncio.sleep(self.interval_seconds)

    async def run_cycle(self) -> dict[str, Any] | None:
        if not self.enabled or not self._is_leader():
            return None

        today = self._now().astimezone(UTC).date()
        first = today - timedelta(days=self.lookback_days)
        tieout = await asyncio.to_thread(self.repository.tieout, first, today)
        self._set_tieout_gauges(tieout, today)

        positions = await asyncio.to_thread(self.repository.positions_tieout)
        OPEN_POSITION_VARIANCE.set(len(positions["phantom_rows"]))
        return tieout

    @staticmethod
    def _set_tieout_gauges(result: dict[str, Any], today: date) -> None:
        variances = [float(day["unexplained"]) for day in result["days"]]
        latest = variances[-1] if variances else 0.0
        worst = max(variances, key=abs, default=0.0)
        TIEOUT_VARIANCE.labels(window="latest").set(latest)
        TIEOUT_VARIANCE.labels(window="worst_in_window").set(worst)
        TIEOUT_CUMULATIVE.set(float(result["cumulative_variance"]))
        TIEOUT_LAST_COMPLETE_DAY.set(
            datetime.combine(
                today - timedelta(days=1), datetime.min.time(), UTC
            ).timestamp()
        )
