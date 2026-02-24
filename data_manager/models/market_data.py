"""
Market data models for candles, trades, order books, and funding rates.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class Candle(BaseModel):
    """OHLCV candle data."""

    symbol: str = Field(..., description="Trading pair symbol")
    timestamp: datetime = Field(..., description="Candle timestamp")
    open: Decimal = Field(..., description="Open price")
    high: Decimal = Field(..., description="High price")
    low: Decimal = Field(..., description="Low price")
    close: Decimal = Field(..., description="Close price")
    volume: Decimal = Field(..., description="Trading volume")
    quote_volume: Decimal | None = Field(None, description="Quote asset volume")
    trades_count: int | None = Field(None, description="Number of trades")
    timeframe: str = Field(..., description="Timeframe (e.g., '1m', '1h')")

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class Trade(BaseModel):
    """Individual trade data."""

    symbol: str = Field(..., description="Trading pair symbol")
    trade_id: int = Field(..., description="Trade ID")
    timestamp: datetime = Field(..., description="Trade timestamp")
    price: Decimal = Field(..., description="Trade price")
    quantity: Decimal = Field(..., description="Trade quantity")
    quote_quantity: Decimal = Field(..., description="Quote quantity")
    is_buyer_maker: bool = Field(..., description="Whether buyer is market maker")
    side: str = Field(..., description="Trade side (buy/sell)")

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class OrderBookLevel(BaseModel):
    """Single order book level."""

    price: Decimal = Field(..., description="Price level")
    quantity: Decimal = Field(..., description="Quantity at this level")

    class Config:
        json_encoders = {Decimal: str}


class OrderBookDepth(BaseModel):
    """Order book depth data."""

    symbol: str = Field(..., description="Trading pair symbol")
    timestamp: datetime = Field(..., description="Snapshot timestamp")
    bids: list[OrderBookLevel] = Field(..., description="Bid levels")
    asks: list[OrderBookLevel] = Field(..., description="Ask levels")
    last_update_id: int | None = Field(None, description="Last update ID")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class FundingRate(BaseModel):
    """Funding rate data for futures."""

    symbol: str = Field(..., description="Trading pair symbol")
    timestamp: datetime = Field(..., description="Funding timestamp")
    funding_rate: Decimal = Field(..., description="Funding rate")
    mark_price: Decimal | None = Field(None, description="Mark price")
    next_funding_time: datetime | None = Field(None, description="Next funding time")

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class MarkPrice(BaseModel):
    """Mark price data for futures."""

    symbol: str = Field(..., description="Trading pair symbol")
    timestamp: datetime = Field(..., description="Mark price timestamp")
    mark_price: Decimal = Field(..., description="Mark price")
    index_price: Decimal | None = Field(None, description="Index price")
    estimated_settle_price: Decimal | None = Field(
        None, description="Estimated settle price"
    )
    last_funding_rate: Decimal | None = Field(None, description="Last funding rate")

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class Ticker(BaseModel):
    """24-hour ticker statistics."""

    symbol: str = Field(..., description="Trading pair symbol")
    timestamp: datetime = Field(..., description="Ticker timestamp")
    open_price: Decimal = Field(..., description="Open price")
    high_price: Decimal = Field(..., description="High price")
    low_price: Decimal = Field(..., description="Low price")
    close_price: Decimal = Field(..., description="Close/Last price")
    volume: Decimal = Field(..., description="Total trading volume")
    quote_volume: Decimal = Field(..., description="Total quote volume")
    price_change: Decimal = Field(..., description="Price change")
    price_change_percent: Decimal = Field(..., description="Price change percentage")
    trades_count: int = Field(..., description="Number of trades")

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}
