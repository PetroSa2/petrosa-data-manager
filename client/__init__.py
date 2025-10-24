"""
Petrosa Data Manager Client Library

A reusable Python client for interacting with the Petrosa Data Manager API.
Supports both generic CRUD operations and domain-specific market data endpoints.
"""

from .client import DataManagerClient
from .exceptions import (
    APIError,
    ConnectionError,
    DataManagerError,
    TimeoutError,
    ValidationError,
)
from .models import (
    CandleData,
    DeleteOptions,
    DepthData,
    FundingData,
    InsertOptions,
    QueryOptions,
    TradeData,
    UpdateOptions,
)

__version__ = "1.0.0"
__all__ = [
    "DataManagerClient",
    "DataManagerError",
    "ConnectionError",
    "TimeoutError",
    "ValidationError",
    "APIError",
    "CandleData",
    "TradeData",
    "DepthData",
    "FundingData",
    "QueryOptions",
    "InsertOptions",
    "UpdateOptions",
    "DeleteOptions",
]
