"""
Data models for Data Manager Client requests and responses.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class QueryOptions(BaseModel):
    """Options for querying data."""

    filter: dict[str, Any] | None = Field(None, description="Query filter conditions")
    sort: dict[str, int] | None = Field(None, description="Sort specification")
    limit: int = Field(100, ge=1, le=1000, description="Maximum records to return")
    offset: int = Field(0, ge=0, description="Number of records to skip")
    fields: list[str] | None = Field(None, description="Fields to include in response")


class InsertOptions(BaseModel):
    """Options for inserting data."""

    data: Union[dict[str, Any], list[dict[str, Any]]] = Field(
        ..., description="Data to insert"
    )
    schema: str | None = Field(None, description="Schema name for validation")
    validate: bool = Field(False, description="Enable schema validation")


class UpdateOptions(BaseModel):
    """Options for updating data."""

    filter: dict[str, Any] = Field(
        ..., description="Filter to identify records to update"
    )
    data: dict[str, Any] = Field(..., description="Data to update")
    upsert: bool = Field(False, description="Create record if not found")
    schema: str | None = Field(None, description="Schema name for validation")
    validate: bool = Field(False, description="Enable schema validation")


class DeleteOptions(BaseModel):
    """Options for deleting data."""

    filter: dict[str, Any] = Field(
        ..., description="Filter to identify records to delete"
    )


class CandleData(BaseModel):
    """OHLCV candle data model."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal | None = None
    trades_count: int | None = None

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class TradeData(BaseModel):
    """Trade execution data model."""

    timestamp: datetime
    trade_id: int
    price: Decimal
    quantity: Decimal
    side: str

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class DepthData(BaseModel):
    """Order book depth data model."""

    bids: list[list[str]]  # [[price, quantity], ...]
    asks: list[list[str]]  # [[price, quantity], ...]
    last_update_id: int


class FundingData(BaseModel):
    """Funding rate data model."""

    timestamp: datetime
    funding_rate: Decimal
    mark_price: Decimal | None = None

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


class PaginationInfo(BaseModel):
    """Pagination metadata."""

    total: int
    limit: int
    offset: int
    page: int
    pages: int
    has_next: bool
    has_previous: bool


class APIResponse(BaseModel):
    """Generic API response wrapper."""

    data: Any
    pagination: PaginationInfo | None = None
    metadata: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None
