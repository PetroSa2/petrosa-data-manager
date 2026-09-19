"""
In-memory backfill request queue — fallback when the backfill orchestrator
is unavailable.

When ``ENABLE_AUTO_BACKFILL=true`` but the orchestrator has not started
(or crashed), gaps are detected but never filled.  This queue bridges that
gap: requests are collected here and flushed to the orchestrator once it
becomes available again.

Usage
-----
    from data_manager.services.backfill_queue import BackfillRequestQueue

    queue = BackfillRequestQueue(max_size=100)

    # In gap_detector._trigger_backfill():
    if self.backfill_orchestrator:
        await self.backfill_orchestrator.create_backfill_job(request)
    else:
        await queue.add(request)

    # In scheduler periodic flush:
    await queue.flush(self.backfill_orchestrator)

Metrics
-------
* ``data_manager_backfill_queue_size`` — gauge of queued requests
* ``data_manager_backfill_queue_flushed_total`` — counter of successfully flushed
* ``data_manager_backfill_queue_failed_total`` — counter of flush failures
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge

from data_manager.models.events import BackfillRequest

logger = logging.getLogger(__name__)

# Prometheus metrics
backfill_queue_size = Gauge(
    "data_manager_backfill_queue_size",
    "Number of backfill requests waiting in the in-memory queue",
)
backfill_queue_flushed_total = Counter(
    "data_manager_backfill_queue_flushed_total",
    "Total backfill requests successfully flushed from the queue to the orchestrator",
    ["symbol", "timeframe"],
)
backfill_queue_failed_total = Counter(
    "data_manager_backfill_queue_failed_total",
    "Total backfill requests that failed to flush (will remain queued for next flush)",
)
gaps_queued_total = Counter(
    "data_manager_gaps_queued_total",
    "Total gaps queued for later backfill (orchestrator unavailable)",
    ["symbol", "timeframe"],
)

# Shared metric: gaps auto-filled (imported by gap_detector and streaming_gap_detector).
gaps_filled_auto_total = Counter(
    "data_manager_gaps_filled_auto_total",
    "Total gaps auto-filled (auto vs manual fill ratio)",
    ["symbol", "timeframe"],
)


class BackfillRequestQueue:
    """Async-safe FIFO queue for backfill requests."""

    def __init__(
        self,
        max_size: int = 100,
        max_retries: int = 3,
    ):
        self._queue: deque[dict] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
        self._max_size = max_size
        self._max_retries = max_retries

    @property
    def size(self) -> int:
        return len(self._queue)

    async def add(self, request: BackfillRequest) -> None:
        """
        Add a backfill request to the queue.

        If the queue is full, the oldest request is dropped (with a warning).
        """
        async with self._lock:
            if len(self._queue) >= self._max_size:
                dropped = self._queue.popleft()
                logger.warning(
                    f"Backfill queue full ({self._max_size}); "
                    f"dropping oldest request for {dropped['request'].symbol} "
                    f"{dropped['request'].timeframe}"
                )
                backfill_queue_failed_total.inc()

            self._queue.append(
                {
                    "request": request,
                    "added_at": datetime.now(UTC),
                    "retries": 0,
                }
            )
            backfill_queue_size.set(len(self._queue))
            logger.debug(
                f"Queued backfill for {request.symbol} {request.timeframe} "
                f"({request.start_time.isoformat()} to {request.end_time.isoformat()})"
            )

    async def flush(
        self,
        backfill_orchestrator,
    ) -> int:
        """
        Flush all queued requests to the orchestrator.

        Requests that fail are retried up to ``max_retries`` times.
        Requests exceeding the retry limit remain in the queue.

        Returns:
            Number of requests successfully flushed.
        """
        if not backfill_orchestrator:
            logger.debug("No orchestrator available; skipping queue flush")
            return 0

        flushed = 0
        async with self._lock:
            if not self._queue:
                return 0

            # Snapshot the queue; process outside the lock
            snapshot = list(self._queue)
            self._queue.clear()
            backfill_queue_size.set(0)

        for item in snapshot:
            request = item["request"]
            try:
                job = await backfill_orchestrator.create_backfill_job(request)
                flushed += 1
                backfill_queue_flushed_total.labels(
                    symbol=request.symbol,
                    timeframe=request.timeframe or "unknown",
                ).inc()
                logger.info(
                    f"Flushed queued backfill: {job.job_id} for "
                    f"{request.symbol} {request.timeframe}"
                )
            except Exception as e:
                item["retries"] += 1
                if item["retries"] >= self._max_retries:
                    logger.error(
                        f"Backfill request for {request.symbol} {request.timeframe} "
                        f"failed {item['retries']} times; dropping"
                    )
                    backfill_queue_failed_total.inc()
                else:
                    # Re-queue for next flush attempt
                    async with self._lock:
                        self._queue.append(item)
                    logger.warning(
                        f"Flush failed for {request.symbol} {request.timeframe}; "
                        f"will retry (attempt {item['retries']}/{self._max_retries})"
                    )
                    backfill_queue_failed_total.inc()

        return flushed

    async def get_status(self) -> dict:
        """Return queue status for health checks."""
        async with self._lock:
            oldest = None
            newest = None
            for item in self._queue:
                ts = item["added_at"]
                if oldest is None or ts < oldest:
                    oldest = ts
                if newest is None or ts > newest:
                    newest = ts
            return {
                "size": len(self._queue),
                "max_size": self._max_size,
                "oldest_age_seconds": (
                    (datetime.now(UTC) - oldest).total_seconds() if oldest else None
                ),
                "newest_age_seconds": (
                    (datetime.now(UTC) - newest).total_seconds() if newest else None
                ),
            }
