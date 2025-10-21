"""
Message handler for routing market data events to appropriate processors.
"""

import logging
from decimal import Decimal

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import (
    CandleRepository,
    DepthRepository,
    FundingRepository,
    TickerRepository,
    TradeRepository,
)
from data_manager.models.events import EventType, MarketDataEvent
from data_manager.models.market_data import (
    Candle,
    FundingRate,
    OrderBookDepth,
    OrderBookLevel,
    Ticker,
    Trade,
)

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

        # Repositories - will be initialized if db_manager provided
        self.trade_repo: TradeRepository | None = None
        self.candle_repo: CandleRepository | None = None
        self.depth_repo: DepthRepository | None = None
        self.funding_repo: FundingRepository | None = None
        self.ticker_repo: TickerRepository | None = None

    async def initialize(self) -> None:
        """Initialize the message handler."""
        logger.info("Initializing message handler")

        # Initialize repositories if database manager provided
        if self.db_manager and self.db_manager.mysql_adapter and self.db_manager.mongodb_adapter:
            self.trade_repo = TradeRepository(
                self.db_manager.mysql_adapter, self.db_manager.mongodb_adapter
            )
            self.candle_repo = CandleRepository(
                self.db_manager.mysql_adapter, self.db_manager.mongodb_adapter
            )
            self.depth_repo = DepthRepository(
                self.db_manager.mysql_adapter, self.db_manager.mongodb_adapter
            )
            self.funding_repo = FundingRepository(
                self.db_manager.mysql_adapter, self.db_manager.mongodb_adapter
            )
            self.ticker_repo = TickerRepository(
                self.db_manager.mysql_adapter, self.db_manager.mongodb_adapter
            )
            logger.info("Repositories initialized successfully")
        else:
            logger.warning("Database manager not available, running without storage")

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
            # Log at debug level - these are expected invalid messages from upstream
            logger.debug(
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
        """Handle trade event."""
        self._stats["trades"] += 1
        logger.debug(
            "Processing trade event",
            extra={
                "symbol": event.symbol,
                "price": event.data.get("p"),
                "quantity": event.data.get("q"),
            },
        )

        # Store trade data in database
        if self.trade_repo:
            try:
                trade = Trade(
                    symbol=event.symbol,
                    trade_id=int(event.data.get("t", 0)),
                    timestamp=event.timestamp,
                    price=Decimal(str(event.data.get("p", "0"))),
                    quantity=Decimal(str(event.data.get("q", "0"))),
                    quote_quantity=Decimal(str(event.data.get("q", "0")))
                    * Decimal(str(event.data.get("p", "0"))),
                    is_buyer_maker=event.data.get("m", False),
                    side="sell" if event.data.get("m") else "buy",
                )
                await self.trade_repo.insert(trade)
                logger.debug(f"Stored trade for {event.symbol}")
            except Exception as e:
                logger.debug(f"Failed to store trade: {e}")  # Database may not be ready

    async def _handle_ticker(self, event: MarketDataEvent) -> None:
        """Handle ticker event."""
        self._stats["tickers"] += 1
        logger.debug(
            "Processing ticker event",
            extra={
                "symbol": event.symbol,
                "close_price": event.data.get("c"),
                "volume": event.data.get("v"),
            },
        )

        # Store ticker data in database
        if self.ticker_repo:
            try:
                ticker = Ticker(
                    symbol=event.symbol,
                    timestamp=event.timestamp,
                    open_price=Decimal(str(event.data.get("o", "0"))),
                    high_price=Decimal(str(event.data.get("h", "0"))),
                    low_price=Decimal(str(event.data.get("l", "0"))),
                    close_price=Decimal(str(event.data.get("c", "0"))),
                    volume=Decimal(str(event.data.get("v", "0"))),
                    quote_volume=Decimal(str(event.data.get("q", "0"))),
                    price_change=Decimal(str(event.data.get("p", "0"))),
                    price_change_percent=Decimal(str(event.data.get("P", "0"))),
                    trades_count=int(event.data.get("n", 0)),
                )
                await self.ticker_repo.insert(ticker)
                logger.debug(f"Stored ticker for {event.symbol}")
            except Exception as e:
                logger.debug(f"Failed to store ticker: {e}")  # Database may not be ready

    async def _handle_depth(self, event: MarketDataEvent) -> None:
        """Handle order book depth event."""
        self._stats["depth"] += 1
        logger.debug(
            "Processing depth event",
            extra={
                "symbol": event.symbol,
                "bids": len(event.data.get("b", [])),
                "asks": len(event.data.get("a", [])),
            },
        )

        # Store depth data in database
        if self.depth_repo:
            try:
                bids = [
                    OrderBookLevel(price=Decimal(str(b[0])), quantity=Decimal(str(b[1])))
                    for b in event.data.get("b", [])[:20]  # Top 20 levels
                ]
                asks = [
                    OrderBookLevel(price=Decimal(str(a[0])), quantity=Decimal(str(a[1])))
                    for a in event.data.get("a", [])[:20]  # Top 20 levels
                ]

                depth = OrderBookDepth(
                    symbol=event.symbol,
                    timestamp=event.timestamp,
                    bids=bids,
                    asks=asks,
                    last_update_id=event.data.get("u"),
                )
                await self.depth_repo.insert(depth)
                logger.debug(f"Stored depth for {event.symbol}")
            except Exception as e:
                logger.debug(f"Failed to store depth: {e}")  # Database may not be ready

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
        """Handle funding rate event."""
        self._stats["funding_rate"] += 1
        logger.debug(
            "Processing funding rate event",
            extra={
                "symbol": event.symbol,
                "funding_rate": event.data.get("r"),
            },
        )

        # Store funding rate data in database
        if self.funding_repo:
            try:
                funding = FundingRate(
                    symbol=event.symbol,
                    timestamp=event.timestamp,
                    funding_rate=Decimal(str(event.data.get("r", "0"))),
                    mark_price=(
                        Decimal(str(event.data.get("p", "0"))) if event.data.get("p") else None
                    ),
                )
                await self.funding_repo.insert(funding)
                logger.debug(f"Stored funding rate for {event.symbol}")
            except Exception as e:
                logger.debug(f"Failed to store funding rate: {e}")  # Database may not be ready

    async def _handle_candle(self, event: MarketDataEvent) -> None:
        """Handle candle/kline event."""
        self._stats["candles"] += 1
        kline = event.data.get("k", {})
        logger.debug(
            "Processing candle event",
            extra={
                "symbol": event.symbol,
                "open": kline.get("o"),
                "close": kline.get("c"),
                "volume": kline.get("v"),
            },
        )

        # Store candle data in database
        if self.candle_repo and kline:
            try:
                candle = Candle(
                    symbol=event.symbol,
                    timestamp=event.timestamp,
                    open=Decimal(str(kline.get("o", "0"))),
                    high=Decimal(str(kline.get("h", "0"))),
                    low=Decimal(str(kline.get("l", "0"))),
                    close=Decimal(str(kline.get("c", "0"))),
                    volume=Decimal(str(kline.get("v", "0"))),
                    quote_volume=Decimal(str(kline.get("q", "0"))) if kline.get("q") else None,
                    trades_count=int(kline.get("n", 0)) if kline.get("n") else None,
                    timeframe=kline.get("i", "1m"),  # Default to 1m if not specified
                )
                await self.candle_repo.insert(candle)
                logger.debug(f"Stored candle for {event.symbol}")
            except Exception as e:
                logger.debug(f"Failed to store candle: {e}")  # Database may not be ready

    async def _handle_unknown(self, event: MarketDataEvent) -> None:
        """Handle unknown event type."""
        self._stats["unknown"] += 1
        # Log at debug level - unknown event types are expected from upstream
        logger.debug(
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
