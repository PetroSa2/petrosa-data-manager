"""
NATS event message models.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Union

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
    data: Dict[str, any] = Field(..., description="Event data payload")
    exchange: str = Field(default="binance", description="Exchange name")
    stream: Optional[str] = Field(None, description="Stream name")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}

    @staticmethod
    def from_nats_message(msg_data: dict) -> "MarketDataEvent":
        """Parse NATS message into MarketDataEvent."""
        # Determine event type from message
        event_type = EventType.UNKNOWN
        if "e" in msg_data:
            event_name = msg_data.get("e", "").lower()
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
        elif "stream" in msg_data:
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

        # Extract symbol
        symbol = msg_data.get("s", msg_data.get("symbol", "UNKNOWN"))

        # Extract timestamp (try multiple fields)
        timestamp_ms = msg_data.get("E", msg_data.get("T", msg_data.get("t", 0)))
        if isinstance(timestamp_ms, int) and timestamp_ms > 0:
            timestamp = datetime.fromtimestamp(timestamp_ms / 1000.0)
        else:
            timestamp = datetime.utcnow()

        return MarketDataEvent(
            event_type=event_type,
            symbol=symbol,
            timestamp=timestamp,
            data=msg_data,
            stream=msg_data.get("stream"),
        )


class BackfillRequest(BaseModel):
    """Request for data backfilling."""

    symbol: str = Field(..., description="Trading pair symbol")
    data_type: str = Field(..., description="Data type (candles/trades/funding)")
    timeframe: Optional[str] = Field(None, description="Timeframe for candles")
    start_time: datetime = Field(..., description="Backfill start time")
    end_time: datetime = Field(..., description="Backfill end time")
    priority: int = Field(default=5, ge=1, le=10, description="Priority (1=highest, 10=lowest)")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class BackfillJob(BaseModel):
    """Backfill job tracking."""

    job_id: str = Field(..., description="Job identifier")
    request: BackfillRequest = Field(..., description="Backfill request details")
    status: str = Field(..., description="Job status (pending/running/completed/failed)")
    progress: float = Field(default=0.0, ge=0.0, le=100.0, description="Progress percentage")
    records_fetched: int = Field(default=0, description="Number of records fetched")
    records_inserted: int = Field(default=0, description="Number of records inserted")
    error_message: Optional[str] = Field(None, description="Error message if failed")
    started_at: Optional[datetime] = Field(None, description="Job start timestamp")
    completed_at: Optional[datetime] = Field(None, description="Job completion timestamp")
    created_at: datetime = Field(..., description="Job creation timestamp")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}

