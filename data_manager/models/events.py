"""
NATS event message models.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Market data event types."""

    TRADE = "trade"
    TICKER = "ticker"
    DEPTH = "depth"
    MARK_PRICE = "markPrice"
    FUNDING_RATE = "fundingRate"
    CANDLE = "kline"
    UNKNOWN = "unknown"


class MarketDataEvent(BaseModel):
    """Generic market data event from NATS."""

    event_type: EventType = Field(..., description="Type of market data event")
    symbol: str = Field(..., description="Trading pair symbol")
    timestamp: datetime = Field(..., description="Event timestamp")
    data: dict[str, Any] = Field(..., description="Event data payload")
    exchange: str = Field(default="binance", description="Exchange name")
    stream: str | None = Field(None, description="Stream name")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}

    @staticmethod
    def from_nats_message(msg_data: dict) -> "MarketDataEvent | None":
        """
        Parse NATS message into MarketDataEvent.

        Returns None if message is invalid (missing symbol).
        """
        import logging

        logger = logging.getLogger(__name__)

        # Handle socket-client message format: {"stream": "...", "data": {...}}
        # The actual Binance data is nested inside the "data" field
        actual_data = msg_data.get(
            "data", msg_data
        )  # Use nested data if present, otherwise use top-level

        # Determine event type from message
        event_type = EventType.UNKNOWN
        if "e" in actual_data:
            event_name = actual_data.get("e", "").lower()
            if event_name == "trade" or event_name == "aggtrade":
                event_type = EventType.TRADE
            elif event_name == "24hrticker":
                event_type = EventType.TICKER
            elif event_name == "depthlevel" or event_name == "depthupdate":
                event_type = EventType.DEPTH
            elif event_name == "markpriceupdpdate":
                event_type = EventType.MARK_PRICE
            elif event_name == "kline":
                event_type = EventType.CANDLE
        elif "stream" in msg_data:  # Stream is at top level in socket-client format
            stream = msg_data.get("stream", "").lower()
            if "trade" in stream:
                event_type = EventType.TRADE
            elif "ticker" in stream:
                event_type = EventType.TICKER
            elif "depth" in stream:
                event_type = EventType.DEPTH
            elif "markprice" in stream:
                event_type = EventType.MARK_PRICE
            elif "fundingrate" in stream:
                event_type = EventType.FUNDING_RATE
            elif "kline" in stream:
                event_type = EventType.CANDLE

        # Extract symbol - try multiple locations:
        # 1. From nested data field (trade, ticker, kline messages)
        # 2. From top level (legacy format)
        # 3. From stream name (depth, markPrice, fundingRate messages)
        symbol = actual_data.get("s", actual_data.get("symbol", msg_data.get("symbol")))

        # If no symbol in data, extract from stream name
        # e.g., "btcusdt@depth20@100ms" -> "BTCUSDT"
        if not symbol and "stream" in msg_data:
            stream = msg_data.get("stream", "")
            if "@" in stream:
                symbol_part = stream.split("@")[
                    0
                ]  # Get "btcusdt" from "btcusdt@depth20@100ms"
                symbol = symbol_part.upper()  # Convert to "BTCUSDT"

        if not symbol or symbol == "UNKNOWN" or not isinstance(symbol, str):
            # This shouldn't happen now - log if it does
            logger.debug(
                f"Skipping message - no symbol found: stream={msg_data.get('stream', 'NONE')}"
            )
            return None

        # Extract timestamp (try multiple fields from both levels)
        timestamp_ms = actual_data.get(
            "E", actual_data.get("T", actual_data.get("t", 0))
        )
        if isinstance(timestamp_ms, int) and timestamp_ms > 0:
            timestamp = datetime.fromtimestamp(timestamp_ms / 1000.0)
        else:
            timestamp = datetime.utcnow()

        return MarketDataEvent(
            event_type=event_type,
            symbol=symbol,
            timestamp=timestamp,
            data=actual_data,  # Use the actual nested data, not the wrapper
            stream=msg_data.get("stream"),
        )


class BackfillRequest(BaseModel):
    """Request for data backfilling."""

    symbol: str = Field(..., description="Trading pair symbol")
    data_type: str = Field(..., description="Data type (candles/trades/funding)")
    timeframe: str | None = Field(None, description="Timeframe for candles")
    start_time: datetime = Field(..., description="Backfill start time")
    end_time: datetime = Field(..., description="Backfill end time")
    priority: int = Field(
        default=5, ge=1, le=10, description="Priority (1=highest, 10=lowest)"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class BackfillJob(BaseModel):
    """Backfill job tracking."""

    job_id: str = Field(..., description="Job identifier")
    request: BackfillRequest = Field(..., description="Backfill request details")
    status: str = Field(
        ..., description="Job status (pending/running/completed/failed)"
    )
    progress: float = Field(
        default=0.0, ge=0.0, le=100.0, description="Progress percentage"
    )
    records_fetched: int = Field(default=0, description="Number of records fetched")
    records_inserted: int = Field(default=0, description="Number of records inserted")
    error_message: str | None = Field(None, description="Error message if failed")
    started_at: datetime | None = Field(None, description="Job start timestamp")
    completed_at: datetime | None = Field(None, description="Job completion timestamp")
    created_at: datetime = Field(..., description="Job creation timestamp")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
