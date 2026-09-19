"""
Streaming (event-driven) gap detector for candle data.

Subscribes to NATS kline events and detects gaps in real-time by
tracking the last-seen timestamp per (symbol, timeframe) pair.
When a gap is detected, it immediately triggers a backfill request
via the configured backfill orchestrator.

This replaces the batch-style gap detection in scheduler.py (which
runs on a 15-minute interval) with near-real-time detection that
typically fires within 1-2 candle intervals after the gap begins.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from prometheus_client import Counter, Gauge, Histogram

import constants
from data_manager.db.repositories import CandleRepository
from data_manager.models.events import BackfillRequest, EventType, MarketDataEvent
from data_manager.services.backfill_queue import (
    BackfillRequestQueue,
    gaps_filled_auto_total,
    gaps_queued_total,
)
from data_manager.utils.time_utils import as_aware_utc, parse_timeframe_to_seconds

logger = logging.getLogger(__name__)

# Prometheus metrics
gap_detection_latency = Histogram(
    "data_manager_gap_detection_latency_seconds",
    "Time from gap start to detection in seconds",
    ["symbol", "timeframe"],
)
gaps_detected_total = Counter(
    "data_manager_gaps_detected_total",
    "Total gaps detected by streaming detector",
    ["symbol", "timeframe", "source"],
)
backfills_triggered_total = Counter(
    "data_manager_backfills_triggered_total",
    "Total backfills triggered by streaming detector",
    ["symbol", "timeframe", "source"],
)

streaming_detector_status = Gauge(
    "data_manager_streaming_detector_status",
    "Streaming detector status (1=active, 0=inactive)",
    ["symbol", "timeframe"],
)
last_seen_timestamp = Gauge(
    "data_manager_last_seen_candle_timestamp",
    "Last seen candle timestamp per symbol/timeframe",
    ["symbol", "timeframe"],
)

# Gap tolerance: a gap is declared when the time between consecutive
# candles exceeds this many intervals. 2 intervals means we tolerate
# one missed tick plus jitter before declaring a gap.
STREAMING_GAP_TOLERANCE_INTERVALS = 2

# Minimum gap duration (seconds) below which we skip backfill to
# avoid noisy micro-gaps from jitter. Must be greater than the
# tolerance window (STREAMING_GAP_TOLERANCE_INTERVALS * interval)
# so that it acts as a secondary filter for gaps that were
# detected but are borderline.
MIN_GAP_DURATION_SECONDS = 180  # 3 minutes

# Maximum gap window to backfill in one request (seconds). Prevents
# the backfill orchestrator from being asked to fill months of data
# if the service was down for a long time.
MAX_BACKFILL_WINDOW_SECONDS = 86400  # 24 hours


class StreamingGapDetector:
    """
    Event-driven gap detector that subscribes to kline events.

    Tracks the last-seen candle timestamp per (symbol, timeframe) pair.
    On each incoming kline event, computes the gap between the received
    timestamp and the expected next timestamp from the previous candle.
    If the gap exceeds the tolerance threshold, a backfill is triggered.
    """

    def __init__(
        self,
        candle_repo: CandleRepository,
        backfill_orchestrator=None,
        nats_client=None,
        subscription_subject: str | None = None,
        backfill_queue: BackfillRequestQueue | None = None,
    ):
        """
        Initialize the streaming gap detector.

        Args:
            candle_repo: Repository for candle data operations.
            backfill_orchestrator: Optional orchestrator to trigger backfills.
            nats_client: NATS client for subscribing to kline events.
            subscription_subject: NATS subject to subscribe to. Defaults
                to constants.NATS_CONSUMER_SUBJECT.
            backfill_queue: Optional in-memory queue for when orchestrator is
                unavailable (petrosa-data-manager#320).
        """
        self.candle_repo = candle_repo
        self.backfill_orchestrator = backfill_orchestrator
        self.nats_client = nats_client
        self.subscription_subject = (
            subscription_subject or constants.NATS_CONSUMER_SUBJECT
        )
        self.backfill_queue = backfill_queue
        self.running = False
        self._subscription = None
        # Last seen timestamp per (symbol, timeframe)
        self._last_seen: dict[tuple[str, str], datetime] = {}
        # Track which gaps we've already reported to avoid duplicates
        self._reported_gaps: set[tuple[str, str, str]] = set()
        self._report_cooldown_seconds = 30  # dedup window per symbol/timeframe
        self._last_report_time: dict[tuple[str, str], float] = defaultdict(float)

    async def start(self) -> bool:
        """
        Start the streaming gap detector.

        Connects to NATS and subscribes to the kline subject.

        Returns:
            True if started successfully, False otherwise.
        """
        if self.running:
            logger.warning("Streaming gap detector already running")
            return True

        if not self.nats_client:
            logger.error("NATS client not configured; cannot start streaming detector")
            return False

        try:
            if not self.nats_client.is_connected():
                connected = await self.nats_client.connect()
                if not connected:
                    logger.error("Failed to connect NATS for streaming detector")
                    return False

            self._subscription = await self.nats_client.subscribe(
                subject=self.subscription_subject,
                callback=self._on_kline_event,
            )

            if self._subscription is None:
                logger.error("Failed to subscribe to NATS subject")
                return False

            self.running = True
            logger.info(
                f"Streaming gap detector started on subject '{self.subscription_subject}'"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to start streaming gap detector: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop the streaming gap detector and unsubscribe."""
        if not self.running:
            return

        self.running = False

        if self._subscription:
            try:
                await self._subscription.unsubscribe()
            except Exception as e:
                logger.warning(f"Error unsubscribing: {e}")

        logger.info("Streaming gap detector stopped")

    def _on_kline_event(self, msg) -> None:
        """
        NATS callback for incoming kline events.

        Parses the message, extracts symbol/timeframe/timestamp,
        and delegates to _process_kline for gap detection.
        """
        try:
            data = json.loads(msg.data.decode())
            # Extract event type to confirm it's a kline
            event_name = data.get("e", "").lower() if isinstance(data, dict) else ""
            if event_name != "kline":
                return

            # _process_kline is async; run it in the background via asyncio
            # so the NATS callback remains non-blocking.
            asyncio.create_task(self._process_kline(data))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse kline event: {e}")
        except Exception as e:
            logger.error(f"Error in kline event handler: {e}", exc_info=True)

    async def _process_kline(self, data: dict) -> None:
        """
        Process a single kline event for gap detection.

        Compares the received candle's close time against the expected
        next timestamp from the previous candle. If a gap is detected,
        triggers a backfill request.

        Args:
            data: Raw kline event data from NATS.
        """
        try:
            kline = data.get("k", {})
            if not kline:
                return

            symbol = data.get("s", "")
            timeframe = kline.get("i", "")
            close_time_str = kline.get("T", kline.get("close_time", None))

            if not symbol or not timeframe or close_time_str is None:
                return

            # Parse the candle close timestamp
            close_time = as_aware_utc(datetime.fromtimestamp(close_time_str / 1000.0))

            key = (symbol, timeframe)
            interval_seconds = parse_timeframe_to_seconds(timeframe)
            if interval_seconds <= 0:
                return

            prev_ts = self._last_seen.get(key)

            if prev_ts is not None:
                # Calculate the expected next candle timestamp
                expected_next_ts = prev_ts + timedelta(seconds=interval_seconds)
                gap_start = expected_next_ts
                gap_duration = (close_time - gap_start).total_seconds()

                # Check if this is a new gap (not a duplicate of a recently reported one)
                gap_id = (symbol, timeframe, gap_start.strftime("%Y-%m-%dT%H:%M:%S"))
                now = time.monotonic()
                last_report = self._last_report_time.get(key, 0)

                if gap_duration > STREAMING_GAP_TOLERANCE_INTERVALS * interval_seconds:
                    # Gap detected
                    logger.info(
                        f"Streaming gap detected: {symbol} {timeframe} "
                        f"gap={gap_duration:.0f}s at {gap_start.isoformat()}"
                    )

                    # Dedup: only report once per cooldown window
                    if now - last_report >= self._report_cooldown_seconds:
                        self._last_report_time[key] = now

                        # Record the latency metric
                        gap_detection_latency.labels(
                            symbol=symbol, timeframe=timeframe
                        ).observe(gap_duration)

                        # Update metrics
                        gaps_detected_total.labels(
                            symbol=symbol, timeframe=timeframe, source="streaming"
                        ).inc()

                        # Update last seen timestamp metric
                        last_seen_timestamp.labels(
                            symbol=symbol, timeframe=timeframe
                        ).set(close_time.timestamp())

                        # Trigger backfill if configured
                        if self.backfill_orchestrator:
                            await self._trigger_backfill(
                                symbol, timeframe, gap_start, close_time, gap_duration
                            )

            # Update last seen timestamp
            self._last_seen[key] = close_time

        except Exception as e:
            logger.warning(f"Error processing kline event: {e}", exc_info=True)

    async def _trigger_backfill(
        self,
        symbol: str,
        timeframe: str,
        gap_start: datetime,
        gap_end: datetime,
        gap_duration: float,
    ) -> None:
        """
        Trigger a backfill for the detected gap.

        Attempts the orchestrator first; if unavailable or the call fails,
        queues the request for later delivery (petrosa-data-manager#320).

        Args:
            symbol: Trading pair symbol.
            timeframe: Candle timeframe.
            gap_start: When the gap started.
            gap_end: The timestamp of the first candle after the gap.
            gap_duration: Duration of the gap in seconds.
        """
        try:
            # Apply minimum gap threshold
            if gap_duration < MIN_GAP_DURATION_SECONDS:
                logger.debug(
                    f"Gap too short for backfill: {gap_duration:.0f}s "
                    f"(min: {MIN_GAP_DURATION_SECONDS}s)"
                )
                return

            # Cap the backfill window
            effective_end = min(
                gap_end,
                gap_start + timedelta(seconds=MAX_BACKFILL_WINDOW_SECONDS),
            )

            request = BackfillRequest(
                symbol=symbol,
                data_type="candles",
                timeframe=timeframe,
                start_time=gap_start,
                end_time=effective_end,
                priority=1,
                source="streaming_gap_detector",
            )

            # Try orchestrator first
            if self.backfill_orchestrator:
                try:
                    job = await self.backfill_orchestrator.create_backfill_job(request)
                    backfills_triggered_total.labels(
                        symbol=symbol, timeframe=timeframe, source="streaming"
                    ).inc()
                    gaps_filled_auto_total.labels(
                        symbol=symbol, timeframe=timeframe
                    ).inc()
                    logger.info(
                        f"Streaming backfill triggered: {job.job_id} for "
                        f"{symbol} {timeframe} ({gap_duration:.0f}s gap)"
                    )
                    return
                except Exception as e:
                    logger.warning(
                        f"Orchestrator call failed for {symbol} {timeframe}: {e}. "
                        "Queuing request for later delivery."
                    )
            else:
                logger.debug("Backfill orchestrator not available; queuing request")

            # Fallback: queue the request
            if self.backfill_queue:
                await self.backfill_queue.add(request)
                gaps_queued_total.labels(symbol=symbol, timeframe=timeframe).inc()
                logger.info(
                    f"Queued streaming backfill for {symbol} {timeframe}: "
                    f"{gap_duration:.0f}s gap "
                    f"(queue size: {self.backfill_queue.size})"
                )
            else:
                logger.warning(
                    f"No backfill orchestrator and no queue available — "
                    f"gap for {symbol} {timeframe} will NOT be filled."
                )

        except Exception as e:
            logger.error(
                f"Failed to trigger backfill for {symbol} {timeframe}: {e}",
                exc_info=True,
            )

    async def get_status(self) -> dict:
        """
        Get the current status of the streaming gap detector.

        Returns:
            Dictionary with status information.
        """
        return {
            "running": self.running,
            "tracked_pairs": len(self._last_seen),
            "last_seen": {
                f"{k[0]}/{k[1]}": v.isoformat()
                for k, v in list(self._last_seen.items())[-10:]
            },
        }
