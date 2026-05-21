"""Periodic strategy-fidelity scan (P2.3, #594).

Iterates known strategies on each tick, runs ``FidelityService.evaluate``
for each, and routes the result through ``FidelityVerdictPublisher``
(which handles hysteresis + the emit decision).
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

from data_manager.strategies.fidelity_publisher import FidelityVerdictPublisher
from data_manager.strategies.fidelity_service import FidelityResult, FidelityService

logger = logging.getLogger(__name__)


class FidelityScheduler:
    """Periodic loop that evaluates fidelity and publishes verdicts."""

    def __init__(
        self,
        *,
        mongodb_adapter: MongoDBAdapter,
        characterization_repository: CharacterizationRepository,
        nats_client: NATSClient,
        interval_seconds: int = 300,
        stable_ticks_required: int = 2,
        strategy_ids: list[str] | None = None,
        threshold: float = FidelityService.DEFAULT_THRESHOLD,
    ) -> None:
        self._mongo = mongodb_adapter
        self._char_repo = characterization_repository
        self._service = FidelityService(
            mongodb_adapter=mongodb_adapter,
            characterization_repository=characterization_repository,
            threshold=threshold,
        )
        self._publisher = FidelityVerdictPublisher(
            nats_client=nats_client,
            stable_ticks_required=stable_ticks_required,
        )
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
            "fidelity_scheduler_started",
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
                    "fidelity_scheduler_cycle_failed", extra={"error": str(exc)}
                )
            await asyncio.sleep(self._interval)

    async def run_cycle(self) -> list[FidelityResult]:
        strategy_ids = await self._discover_strategies()
        results: list[FidelityResult] = []
        for sid in strategy_ids:
            try:
                result = await self._service.evaluate(sid)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "fidelity_cycle_strategy_failed",
                    extra={"strategy_id": sid, "error": str(exc)},
                )
                continue
            results.append(result)
            await self._publisher.maybe_publish(result)
        return results

    async def _discover_strategies(self) -> list[str]:
        if self._static_strategy_ids is not None:
            return list(self._static_strategy_ids)
        try:
            return await self._mongo.list_all_strategy_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fidelity_scheduler_discovery_failed",
                extra={"error": str(exc)},
            )
            return []
