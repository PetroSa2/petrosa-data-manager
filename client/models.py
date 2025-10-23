"""
Data models for Data Manager Client requests and responses.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from decimal import Decimal

from pydantic import BaseModel, Field


class QueryOptions(BaseModel):
    """Options for querying data."""
    
    filter: Optional[Dict[str, Any]] = Field(None, description="Query filter conditions")
    sort: Optional[Dict[str, int]] = Field(None, description="Sort specification")
    limit: int = Field(100, ge=1, le=1000, description="Maximum records to return")
    offset: int = Field(0, ge=0, description="Number of records to skip")
    fields: Optional[List[str]] = Field(None, description="Fields to include in response")


class InsertOptions(BaseModel):
    """Options for inserting data."""
    
    data: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(..., description="Data to insert")
    schema: Optional[str] = Field(None, description="Schema name for validation")
    validate: bool = Field(False, description="Enable schema validation")


class UpdateOptions(BaseModel):
    """Options for updating data."""
    
    filter: Dict[str, Any] = Field(..., description="Filter to identify records to update")
    data: Dict[str, Any] = Field(..., description="Data to update")
    upsert: bool = Field(False, description="Create record if not found")
    schema: Optional[str] = Field(None, description="Schema name for validation")
    validate: bool = Field(False, description="Enable schema validation")


class DeleteOptions(BaseModel):
    """Options for deleting data."""
    
    filter: Dict[str, Any] = Field(..., description="Filter to identify records to delete")


class CandleData(BaseModel):
    """OHLCV candle data model."""
    
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Optional[Decimal] = None
    trades_count: Optional[int] = None
    
    class Config:
        json_encoders = {
            Decimal: str,
            datetime: lambda v: v.isoformat()
        }


class TradeData(BaseModel):
    """Trade execution data model."""
    
    timestamp: datetime
    trade_id: int
    price: Decimal
    quantity: Decimal
    side: str
    
    class Config:
        json_encoders = {
            Decimal: str,
            datetime: lambda v: v.isoformat()
        }


class DepthData(BaseModel):
    """Order book depth data model."""
    
    bids: List[List[str]]  # [[price, quantity], ...]
    asks: List[List[str]]  # [[price, quantity], ...]
    last_update_id: int


class FundingData(BaseModel):
    """Funding rate data model."""
    
    timestamp: datetime
    funding_rate: Decimal
    mark_price: Optional[Decimal] = None
    
    class Config:
        json_encoders = {
            Decimal: str,
            datetime: lambda v: v.isoformat()
        }


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
    pagination: Optional[PaginationInfo] = None
    metadata: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = None

