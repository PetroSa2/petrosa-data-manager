"""
Repository pattern for data access layer.
"""

from data_manager.db.repositories.audit_repository import AuditRepository
from data_manager.db.repositories.backfill_repository import BackfillRepository
from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.db.repositories.candle_repository import CandleRepository
from data_manager.db.repositories.catalog_repository import CatalogRepository
from data_manager.db.repositories.depth_repository import DepthRepository
from data_manager.db.repositories.funding_repository import FundingRepository
from data_manager.db.repositories.health_repository import HealthRepository
from data_manager.db.repositories.ticker_repository import TickerRepository
from data_manager.db.repositories.trade_repository import TradeRepository

__all__ = [
    "BaseRepository",
    "TradeRepository",
    "CandleRepository",
    "DepthRepository",
    "FundingRepository",
    "TickerRepository",
    "AuditRepository",
    "HealthRepository",
    "BackfillRepository",
    "CatalogRepository",
]
