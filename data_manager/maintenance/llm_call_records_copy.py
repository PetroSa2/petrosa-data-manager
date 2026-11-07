"""Leader-only MySQL historic copy of LLM call records (petrosa-data-manager#841).

Every ``COPY_INTERVAL_SECONDS`` the leader copies a batch of ``llm_call_records`` still marked
``_mysql_copy_pending`` to the MySQL ``llm_call_records`` table (insert-only on ``record_key``), then marks them
``mysql_copied_at``. The Mongo TTL only applies after that, so a record never expires before it is in the
history.

A broken copy is visible: ``data_manager_llm_call_records_uncopied_stale`` counts records still uncopied after
``STUCK_AFTER_SECONDS``, and a WARNING names the count. Copy outcomes are counted on
``data_manager_llm_call_records_copy_total{result}``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from typing import Any

from opentelemetry.metrics import CallbackOptions, Observation

from data_manager.services import llm_call_records

try:
    from petrosa_otel import get_meter
except ImportError:  # pragma: no cover - petrosa_otel always present in prod
    from opentelemetry import metrics as _otel_metrics

    get_meter = _otel_metrics.get_meter

logger = logging.getLogger(__name__)

#: Labelled fallbacks, overridable by environment.
COPY_INTERVAL_SECONDS = float(os.getenv("LLM_CALL_RECORDS_COPY_INTERVAL_SECONDS", "60"))
BATCH_SIZE = int(os.getenv("LLM_CALL_RECORDS_COPY_BATCH", "500"))
STUCK_AFTER_SECONDS = float(
    os.getenv("LLM_CALL_RECORDS_STUCK_AFTER_SECONDS", str(6 * 3600))
)
INITIAL_DELAY_SECONDS = float(
    os.getenv("LLM_CALL_RECORDS_COPY_INITIAL_DELAY_SECONDS", "60")
)

_meter = get_meter(__name__)
COPY_TOTAL = _meter.create_counter(
    "data_manager_llm_call_records_copy_total",
    description="LLM call record batches copied to MySQL, by result (copied | empty | failure)",
)
_stale = {"value": None}


def _observe_stale(_options: CallbackOptions) -> Iterable[Observation]:
    if _stale["value"] is not None:
        yield Observation(_stale["value"])


UNCOPIED_STALE = _meter.create_observable_gauge(
    "data_manager_llm_call_records_uncopied_stale",
    callbacks=[_observe_stale],
    description="LLM call records still not copied to MySQL after LLM_CALL_RECORDS_STUCK_AFTER_SECONDS",
)


class LlmCallRecordsCopy:
    """Copies pending records to MySQL while this pod is the leader."""

    def __init__(self, db_manager: Any, leader_election: Any = None) -> None:
        self.db_manager = db_manager
        self.leader_election = leader_election
        self.running = False

    def _is_leader(self) -> bool:
        if not self.leader_election:
            return True
        return bool(getattr(self.leader_election, "is_leader", False))

    async def run_once(self) -> dict[str, int]:
        mongodb = self.db_manager.mongodb_adapter
        mysql = self.db_manager.mysql_adapter
        copied = await llm_call_records.copy_pending(mongodb, mysql, limit=BATCH_SIZE)
        COPY_TOTAL.add(1, {"result": "copied" if copied["copied"] else "empty"})
        stale = await llm_call_records.stuck_uncopied_count(
            mongodb, STUCK_AFTER_SECONDS
        )
        _stale["value"] = stale
        if stale:
            logger.warning(
                "LLM_CALL_RECORDS_COPY_STUCK: %d records not copied to MySQL after %.0f s",
                stale,
                STUCK_AFTER_SECONDS,
            )
        return {**copied, "stale": stale}

    async def start(self) -> None:
        self.running = True
        await asyncio.sleep(INITIAL_DELAY_SECONDS)
        while self.running:
            if self._is_leader():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    COPY_TOTAL.add(1, {"result": "failure"})
                    logger.error(
                        "llm_call_records copy failed: %s: %s", type(exc).__name__, exc
                    )
            await asyncio.sleep(COPY_INTERVAL_SECONDS)
