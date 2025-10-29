"""
NATS client connection manager with reconnection logic.
"""

import json
import logging
from collections.abc import Callable

import nats
import nats.aio.client
from prometheus_client import Counter, Gauge

import constants
from data_manager.utils.nats_trace_propagator import NATSTracePropagator

logger = logging.getLogger(__name__)

# Prometheus metrics
nats_connection_status = Gauge(
    "data_manager_nats_connection_status",
    "NATS connection status (1=connected, 0=disconnected)",
)
nats_reconnections = Counter(
    "data_manager_nats_reconnections_total", "Total NATS reconnection attempts"
)
nats_errors = Counter("data_manager_nats_errors_total", "Total NATS errors", ["type"])


class NATSClient:
    """NATS client with connection management."""

    def __init__(self) -> None:
        self.nc: nats.aio.client.Client | None = None
        self.connected: bool = False
        self._reconnect_cb: Callable | None = None
        self._disconnect_cb: Callable | None = None

    async def connect(self) -> bool:
        """Connect to NATS server."""
        try:
            logger.info(
                "Connecting to NATS",
                extra={
                    "url": constants.NATS_URL,
                    "client_name": constants.NATS_CLIENT_NAME,
                },
            )

            self.nc = await nats.connect(
                servers=[constants.NATS_URL],
                name=constants.NATS_CLIENT_NAME,
                connect_timeout=constants.NATS_CONNECT_TIMEOUT,
                max_reconnect_attempts=constants.NATS_MAX_RECONNECT_ATTEMPTS,
                reconnect_time_wait=constants.NATS_RECONNECT_TIME_WAIT,
                ping_interval=60,
                max_outstanding_pings=3,
                allow_reconnect=True,
                disconnected_cb=self._on_disconnected,
                reconnected_cb=self._on_reconnected,
                error_cb=self._on_error,
            )

            self.connected = True
            nats_connection_status.set(1)

            logger.info(
                "Successfully connected to NATS",
                extra={
                    "url": constants.NATS_URL,
                    "server_info": self.nc.connected_server_version
                    if self.nc
                    else None,
                },
            )
            return True

        except Exception as e:
            logger.error(f"Failed to connect to NATS: {e}", exc_info=True)
            nats_errors.labels(type="connection").inc()
            self.connected = False
            nats_connection_status.set(0)
            return False

    async def disconnect(self) -> None:
        """Disconnect from NATS server."""
        if self.nc and self.connected:
            try:
                await self.nc.drain()
                await self.nc.close()
                logger.info("Disconnected from NATS")
            except Exception as e:
                logger.error(f"Error disconnecting from NATS: {e}")
            finally:
                self.connected = False
                nats_connection_status.set(0)

    async def subscribe(
        self, subject: str, callback: Callable, queue: str | None = None
    ) -> nats.aio.subscription.Subscription | None:
        """Subscribe to a NATS subject."""
        if not self.nc or not self.connected:
            logger.error("Cannot subscribe: not connected to NATS")
            return None

        try:
            logger.info(
                "Subscribing to NATS subject",
                extra={"subject": subject, "queue": queue},
            )

            subscription = await self.nc.subscribe(subject, queue=queue, cb=callback)

            logger.info(
                "Successfully subscribed to NATS subject",
                extra={"subject": subject, "queue": queue},
            )
            return subscription

        except Exception as e:
            logger.error(
                f"Failed to subscribe to subject {subject}: {e}", exc_info=True
            )
            nats_errors.labels(type="subscription").inc()
            return None

    async def publish(self, subject: str, data: bytes) -> bool:
        """Publish message to a NATS subject."""
        if not self.nc or not self.connected:
            logger.error("Cannot publish: not connected to NATS")
            return False

        try:
            await self.nc.publish(subject, data)
            return True
        except Exception as e:
            logger.error(f"Failed to publish to subject {subject}: {e}")
            nats_errors.labels(type="publish").inc()
            return False

    async def publish_with_trace_context(
        self, subject: str, message_dict: dict
    ) -> bool:
        """
        Publish message to a NATS subject with trace context injection.

        This method injects the current OpenTelemetry trace context into the
        message before publishing, enabling distributed tracing across services.

        Args:
            subject: NATS subject to publish to
            message_dict: Message dictionary (will be modified to include trace context)

        Returns:
            True if published successfully, False otherwise

        Example:
            >>> data = {"symbol": "BTCUSDT", "price": 50000}
            >>> await nats_client.publish_with_trace_context("market.data", data)
        """
        if not self.nc or not self.connected:
            logger.error("Cannot publish: not connected to NATS")
            return False

        try:
            # Inject trace context into message
            message_with_trace = NATSTracePropagator.inject_context(message_dict)

            # Serialize to JSON and encode
            data = json.dumps(message_with_trace).encode()

            # Publish message
            await self.nc.publish(subject, data)
            return True

        except Exception as e:
            logger.error(f"Failed to publish to subject {subject}: {e}", exc_info=True)
            nats_errors.labels(type="publish").inc()
            return False

    def is_connected(self) -> bool:
        """Check if connected to NATS."""
        return self.connected and self.nc is not None and self.nc.is_connected

    async def _on_disconnected(self) -> None:
        """Handle disconnection from NATS."""
        logger.warning("Disconnected from NATS")
        self.connected = False
        nats_connection_status.set(0)
        if self._disconnect_cb:
            await self._disconnect_cb()

    async def _on_reconnected(self) -> None:
        """Handle reconnection to NATS."""
        logger.info("Reconnected to NATS")
        self.connected = True
        nats_connection_status.set(1)
        nats_reconnections.inc()
        if self._reconnect_cb:
            await self._reconnect_cb()

    async def _on_error(self, error: Exception) -> None:
        """Handle NATS errors."""
        logger.error(f"NATS error: {error}")
        nats_errors.labels(type="general").inc()

    def set_callbacks(
        self,
        reconnect_cb: Callable | None = None,
        disconnect_cb: Callable | None = None,
    ) -> None:
        """Set callbacks for connection events."""
        self._reconnect_cb = reconnect_cb
        self._disconnect_cb = disconnect_cb
