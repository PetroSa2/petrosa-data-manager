"""
Data models for the Petrosa Data Manager service.
"""

from data_manager.models.analytics import (
    CorrelationMetrics,
    DeviationMetrics,
    MarketRegime,
    SeasonalityMetrics,
    SpreadMetrics,
    TrendMetrics,
    VolatilityMetrics,
    VolumeMetrics,
)
from data_manager.models.catalog import DatasetMetadata, LineageRecord, SchemaDefinition
from data_manager.models.events import EventType, MarketDataEvent
from data_manager.models.health import DataHealthMetrics, DatasetHealth
from data_manager.models.market_data import (
    Candle,
    FundingRate,
    MarkPrice,
    OrderBookDepth,
    Ticker,
    Trade,
)

__all__ = [
    # Market Data
    "Candle",
    "Trade",
    "OrderBookDepth",
    "FundingRate",
    "MarkPrice",
    "Ticker",
    # Health
    "DataHealthMetrics",
    "DatasetHealth",
    # Analytics
    "VolatilityMetrics",
    "VolumeMetrics",
    "SpreadMetrics",
    "DeviationMetrics",
    "TrendMetrics",
    "SeasonalityMetrics",
    "CorrelationMetrics",
    "MarketRegime",
    # Catalog
    "DatasetMetadata",
    "SchemaDefinition",
    "LineageRecord",
    # Events
    "MarketDataEvent",
    "EventType",
]
