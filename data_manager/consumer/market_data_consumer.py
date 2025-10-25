"""
Market data consumer for processing NATS messages.
"""

import asyncio
import json
import logging
from typing import Any

from opentelemetry import trace
from prometheus_client import Counter, Histogram

import constants
from data_manager.consumer.message_handler import MessageHandler
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.events import MarketDataEvent
from data_manager.utils.nats_trace_propagator import NATSTracePropagator

logger = logging.getLogger(__name__)

# Get OpenTelemetry tracer
tracer = trace.get_tracer(__name__)

# Prometheus metrics
messages_received = Counter(
    "data_manager_messages_received_total",
    "Total messages received from NATS",
    ["event_type"],
)
messages_processed = Counter(
    "data_manager_messages_processed_total",
    "Total messages processed successfully",
    ["event_type"],
)
messages_failed = Counter(
    "data_manager_messages_failed_total",
    "Total messages that failed processing",
    ["event_type", "error_type"],
)
message_processing_time = Histogram(
    "data_manager_message_processing_seconds",
    "Message processing time in seconds",
    ["event_type"],
)


class MarketDataConsumer:
    """Consumer for market data events from NATS."""

    def __init__(
        self,
        nats_client: NATSClient | None = None,
        message_handler: MessageHandler | None = None,
        db_manager: Any | None = None,
    ) -> None:
        self.nats_client = nats_client or NATSClient()
        self.db_manager = db_manager
        self.message_handler = message_handler or MessageHandler(db_manager=db_manager)
        self.running = False
        self.subscription = None
        self._message_queue: asyncio.Queue = asyncio.Queue(
            maxsize=constants.MESSAGE_QUEUE_SIZE
        )
        self._processing_tasks: list = []
        self._stats_task: asyncio.Task | None = None
        self._messages_processed = 0
        self._last_stats_time = asyncio.get_event_loop().time()

    async def start(self) -> bool:
        """Start the consumer."""
        try:
            logger.info("Starting market data consumer")

            # Connect to NATS
            if not await self.nats_client.connect():
                logger.error("Failed to connect to NATS")
                return False

            # Initialize message handler
            await self.message_handler.initialize()

            # Subscribe to market data subject
            self.subscription = await self.nats_client.subscribe(
                subject=constants.NATS_CONSUMER_SUBJECT,
                callback=self._on_message,
            )

            if not self.subscription:
                logger.error("Failed to subscribe to NATS subject")
                return False

            # Start message processing workers
            self.running = True
            num_workers = min(constants.MAX_CONCURRENT_TASKS, 10)
            for i in range(num_workers):
                task = asyncio.create_task(self._process_messages_worker(i))
                self._processing_tasks.append(task)

            # Start stats reporting task
            self._stats_task = asyncio.create_task(self._report_stats())

            logger.info(
                "Market data consumer started successfully",
                extra={
                    "subject": constants.NATS_CONSUMER_SUBJECT,
                    "workers": num_workers,
                },
            )
            return True

        except Exception as e:
            logger.error(f"Failed to start market data consumer: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop the consumer."""
        logger.info("Stopping market data consumer")
        self.running = False

        # Stop stats reporting
        if self._stats_task:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass

        # Wait for processing tasks to complete
        if self._processing_tasks:
            await asyncio.gather(*self._processing_tasks, return_exceptions=True)

        # Unsubscribe
        if self.subscription:
            try:
                await self.subscription.unsubscribe()
            except Exception as e:
                logger.error(f"Error unsubscribing: {e}")

        # Disconnect from NATS
        await self.nats_client.disconnect()

        # Shutdown message handler
        await self.message_handler.shutdown()

        logger.info("Market data consumer stopped")

    async def _on_message(self, msg: Any) -> None:
        """Handle incoming NATS message."""
        try:
            # Add message to queue for processing
            await self._message_queue.put(msg)
        except asyncio.QueueFull:
            logger.warning("Message queue full, dropping message")
            messages_failed.labels(event_type="unknown", error_type="queue_full").inc()

    async def _process_messages_worker(self, worker_id: int) -> None:
        """Worker task for processing messages from queue."""
        logger.info(f"Message processing worker {worker_id} started")

        while self.running:
            try:
                # Get message from queue with timeout
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                await self._process_message(msg)
            except TimeoutError:
                # No message available, continue
                continue
            except Exception as e:
                logger.error(
                    f"Error in processing worker {worker_id}: {e}",
                    exc_info=True,
                )
                await asyncio.sleep(1)

        logger.info(f"Message processing worker {worker_id} stopped")

    async def _process_message(self, msg: Any) -> None:
        """Process a single message with trace context propagation."""
        event_type = "unknown"
        start_time = asyncio.get_event_loop().time()

        try:
            # Decode message
            data = json.loads(msg.data.decode())
            messages_received.labels(event_type="raw").inc()

            # Create span from message using NATSTracePropagator utility
            with NATSTracePropagator.create_span_from_message(
                tracer,
                data,
                "process_nats_message",
                span_kind=trace.SpanKind.CONSUMER,
            ) as span:
                # Set additional NATS-specific attributes
                span.set_attribute(
                    "messaging.destination", constants.NATS_CONSUMER_SUBJECT
                )

                # Parse into MarketDataEvent
                event = MarketDataEvent.from_nats_message(data)

                # Skip invalid messages (missing or invalid symbol)
                if event is None:
                    span.set_attribute("message.invalid", True)
                    # DEBUG: Log sample message to understand format
                    logger.warning(
                        "Skipping message with missing or invalid symbol",
                        extra={
                            "data_keys": list(data.keys()),
                            "sample_data": str(data)[
                                :500
                            ],  # First 500 chars of message
                        },
                    )
                    messages_received.labels(event_type="invalid").inc()
                    return

                event_type = event.event_type.value
                messages_received.labels(event_type=event_type).inc()

                # Add event details to span
                span.set_attribute("event.type", event_type)
                span.set_attribute("event.symbol", event.symbol)
                span.set_attribute("event.timestamp", event.timestamp.isoformat())

                logger.debug(
                    "Processing market data event",
                    extra={
                        "event_type": event_type,
                        "symbol": event.symbol,
                        "timestamp": event.timestamp.isoformat(),
                    },
                )

                # Route to appropriate handler
                await self.message_handler.handle_event(event)

                # Track metrics
                processing_time = asyncio.get_event_loop().time() - start_time
                message_processing_time.labels(event_type=event_type).observe(
                    processing_time
                )
                messages_processed.labels(event_type=event_type).inc()
                self._messages_processed += 1

                span.set_attribute("message.processing_time_seconds", processing_time)
                span.set_status(trace.Status(trace.StatusCode.OK))

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON message: {e}")
            messages_failed.labels(
                event_type=event_type, error_type="json_decode"
            ).inc()

        except Exception as e:
            logger.error(f"Failed to process message: {e}", exc_info=True)
            messages_failed.labels(event_type=event_type, error_type="processing").inc()

    async def _report_stats(self) -> None:
        """Periodically report processing statistics at INFO level."""
        stats_interval = 60  # Report every 60 seconds

        while self.running:
            try:
                await asyncio.sleep(stats_interval)

                # Calculate messages per second
                current_time = asyncio.get_event_loop().time()
                time_elapsed = current_time - self._last_stats_time
                messages_per_sec = (
                    self._messages_processed / time_elapsed if time_elapsed > 0 else 0
                )

                # Get handler stats
                handler_stats = self.message_handler.get_stats()

                # Log summary
                logger.info(
                    f"Message processing stats: "
                    f"total={self._messages_processed}, "
                    f"rate={messages_per_sec:.1f} msg/s, "
                    f"trades={handler_stats.get('trades', 0)}, "
                    f"depth={handler_stats.get('depth', 0)}, "
                    f"tickers={handler_stats.get('tickers', 0)}, "
                    f"candles={handler_stats.get('candles', 0)}, "
                    f"queue_size={self._message_queue.qsize()}"
                )

                # Reset counters for next interval
                self._messages_processed = 0
                self._last_stats_time = current_time

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error reporting stats: {e}")

    async def get_stats(self) -> dict:
        """Get consumer statistics."""
        return {
            "running": self.running,
            "connected": self.nats_client.is_connected(),
            "queue_size": self._message_queue.qsize(),
            "workers": len(self._processing_tasks),
        }
