"""
Message handler for routing market data events to appropriate processors.

NOTE: This handler does NOT persist raw market data. It only:
- Tracks message statistics (counts by type)
- Routes events for real-time metrics
- Monitors data flow health

Raw data persistence is handled by binance-data-extractor.
"""

import logging

from data_manager.db.database_manager import DatabaseManager
from data_manager.models.events import EventType, MarketDataEvent

logger = logging.getLogger(__name__)


class MessageHandler:
    """Handler for routing market data events."""

    def __init__(self, db_manager: DatabaseManager | None = None) -> None:
        self.initialized = False
        self.db_manager = db_manager
        self._handlers: dict[EventType, callable] = {}
        self._stats: dict[str, int] = {
            "trades": 0,
            "tickers": 0,
            "depth": 0,
            "mark_price": 0,
            "funding_rate": 0,
            "candles": 0,
            "unknown": 0,
        }

        # NOTE: No repositories needed - data-manager doesn't persist raw market data
        # It only tracks message counts and statistics for monitoring

    async def initialize(self) -> None:
        """Initialize the message handler."""
        logger.info("Initializing message handler")

        # NOTE: Data-manager does NOT persist raw market data
        # - Trades, candles, depth, tickers, funding → binance-data-extractor handles this
        # - Data-manager only tracks metrics and persists analytics/audit results
        # - Repositories are not needed for message handling
        logger.info(
            "Message handler initialized (tracking mode only - no raw data persistence)"
        )

        # Register handlers for each event type
        self._handlers = {
            EventType.TRADE: self._handle_trade,
            EventType.TICKER: self._handle_ticker,
            EventType.DEPTH: self._handle_depth,
            EventType.MARK_PRICE: self._handle_mark_price,
            EventType.FUNDING_RATE: self._handle_funding_rate,
            EventType.CANDLE: self._handle_candle,
        }

        self.initialized = True
        logger.info("Message handler initialized")

    async def shutdown(self) -> None:
        """Shutdown the message handler."""
        logger.info("Shutting down message handler")
        self.initialized = False
        logger.info("Message handler shutdown complete")

    async def handle_event(self, event: MarketDataEvent) -> None:
        """Route event to appropriate handler."""
        if not self.initialized:
            logger.warning("Message handler not initialized")
            return

        # Validate symbol before processing
        if not event.symbol or event.symbol == "UNKNOWN":
            logger.warning(
                "Skipping event with invalid symbol",
                extra={
                    "event_type": event.event_type.value,
                    "symbol": event.symbol,
                },
            )
            self._stats["unknown"] += 1
            return

        try:
            handler = self._handlers.get(event.event_type)
            if handler:
                await handler(event)
            else:
                await self._handle_unknown(event)
        except Exception as e:
            logger.error(
                f"Error handling event {event.event_type}: {e}",
                exc_info=True,
                extra={
                    "event_type": event.event_type.value,
                    "symbol": event.symbol,
                },
            )

    async def _handle_trade(self, event: MarketDataEvent) -> None:
        """
        Handle trade event.

        NOTE: We do NOT persist individual trades - that's the binance-data-extractor's job.
        Data-manager only monitors data quality and computes analytics.
        """
        self._stats["trades"] += 1
        logger.debug(
            "Received trade event",
            extra={
                "symbol": event.symbol,
                "price": event.data.get("p"),
                "quantity": event.data.get("q"),
            },
        )
        # Track metrics only, no persistence

    async def _handle_ticker(self, event: MarketDataEvent) -> None:
        """
        Handle ticker event.

        NOTE: Tickers are 24h summary stats - we track but don't persist them.
        """
        self._stats["tickers"] += 1
        logger.debug(
            "Received ticker event",
            extra={
                "symbol": event.symbol,
                "close_price": event.data.get("c"),
                "volume": event.data.get("v"),
            },
        )
        # Track metrics only, no persistence

    async def _handle_depth(self, event: MarketDataEvent) -> None:
        """
        Handle order book depth event.

        NOTE: Depth data is TOO EXPENSIVE to persist. We only use it for:
        - Real-time spread calculations
        - Liquidity monitoring
        Data is NOT stored long-term.
        """
        self._stats["depth"] += 1
        logger.debug(
            "Received depth event",
            extra={
                "symbol": event.symbol,
                "bids": len(event.data.get("b", [])),
                "asks": len(event.data.get("a", [])),
            },
        )
        # Track metrics only, NO PERSISTENCE (too expensive)

    async def _handle_mark_price(self, event: MarketDataEvent) -> None:
        """Handle mark price event."""
        self._stats["mark_price"] += 1
        logger.debug(
            "Processing mark price event",
            extra={
                "symbol": event.symbol,
                "mark_price": event.data.get("p"),
            },
        )
        # TODO: Store mark price data in database

    async def _handle_funding_rate(self, event: MarketDataEvent) -> None:
        """
        Handle funding rate event.

        NOTE: Funding rates change infrequently (every 8h). These are already
        persisted by binance-data-extractor. We just track them here.
        """
        self._stats["funding_rate"] += 1
        logger.debug(
            "Received funding rate event",
            extra={
                "symbol": event.symbol,
                "funding_rate": event.data.get("r"),
            },
        )
        # Track metrics only, extractor handles persistence

    async def _handle_candle(self, event: MarketDataEvent) -> None:
        """
        Handle candle/kline event.

        NOTE: Candles are already persisted by binance-data-extractor.
        Data-manager reads FROM extractor's database for analytics, not stores its own copy.
        """
        self._stats["candles"] += 1
        kline = event.data.get("k", {})
        logger.debug(
            "Received candle event",
            extra={
                "symbol": event.symbol,
                "open": kline.get("o"),
                "close": kline.get("c"),
                "volume": kline.get("v"),
            },
        )
        # Track metrics only, extractor handles persistence

    async def _handle_unknown(self, event: MarketDataEvent) -> None:
        """Handle unknown event type."""
        self._stats["unknown"] += 1
        logger.warning(
            "Received unknown event type",
            extra={
                "event_type": event.event_type.value,
                "symbol": event.symbol,
                "data_keys": list(event.data.keys()),
            },
        )

    def get_stats(self) -> dict[str, int]:
        """Get handler statistics."""
        return self._stats.copy()
