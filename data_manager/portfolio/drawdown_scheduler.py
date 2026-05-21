"""Periodic drawdown-vs-envelope check (P4.2, #602).

Iterates known strategies on each tick, runs ``DrawdownService.compute``
for each, and emits a breach event via ``DrawdownBreachPublisher`` when
the envelope is exceeded.

Strategy discovery: by default the scheduler asks the MongoDB adapter
for ``list_all_strategy_ids`` (already exists at
``mongodb_adapter.py:638``). Callers can inject a static list for
testing or when the scheduler should be scoped to a subset.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_manager.consumer.nats_client import NATSClient
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.repositories.characterization_repository import (
        CharacterizationRepository,
    )

from data_manager.portfolio.drawdown_publisher import DrawdownBreachPublisher
from data_manager.portfolio.drawdown_service import DrawdownResult, DrawdownService

logger = logging.getLogger(__name__)


class DrawdownScheduler:
    """Periodic loop that checks every strategy and publishes breaches."""

    def __init__(
        self,
        *,
        mongodb_adapter: MongoDBAdapter,
        characterization_repository: CharacterizationRepository,
        nats_client: NATSClient,
        interval_seconds: int = 300,
        strategy_ids: list[str] | None = None,
    ) -> None:
        self._mongo = mongodb_adapter
        self._char_repo = characterization_repository
        self._service = DrawdownService(
            mongodb_adapter=mongodb_adapter,
            characterization_repository=characterization_repository,
        )
        self._publisher = DrawdownBreachPublisher(nats_client=nats_client)
        self._interval = max(1, interval_seconds)
        self._static_strategy_ids = strategy_ids
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "drawdown_scheduler_started",
            extra={"interval_seconds": self._interval},
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_cycle()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.error(
                    "drawdown_scheduler_cycle_failed", extra={"error": str(exc)}
                )
            await asyncio.sleep(self._interval)

    async def run_cycle(self) -> list[DrawdownResult]:
        """One scan of all strategies. Returns the results for testability."""
        strategy_ids = await self._discover_strategies()
        results: list[DrawdownResult] = []
        for sid in strategy_ids:
            try:
                result = await self._service.compute(sid)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "drawdown_cycle_strategy_failed",
                    extra={"strategy_id": sid, "error": str(exc)},
                )
                continue
            results.append(result)
            if result.breached:
                await self._publisher.maybe_publish(result)
        return results

    async def _discover_strategies(self) -> list[str]:
        if self._static_strategy_ids is not None:
            return list(self._static_strategy_ids)
        # `list_all_strategy_ids` is async on the MongoDB adapter.
        try:
            return await self._mongo.list_all_strategy_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "drawdown_scheduler_discovery_failed",
                extra={"error": str(exc)},
            )
            return []
