"""
Repository for funding rate data operations.
"""

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import FundingRate

logger = logging.getLogger(__name__)


class FundingRateMySQLRow(BaseModel):
    """Shape of the existing extractor-owned ``funding_rates`` table."""

    id: str
    symbol: str
    timestamp: datetime
    funding_rate: Decimal
    funding_time: datetime
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    last_funding_rate: Decimal | None = None
    funding_interval_hours: int = 8
    extracted_at: datetime
    extractor_version: str = "data-manager"
    source: str = "data-manager"


class FundingRepository(BaseRepository):
    """Repository for managing funding rate data in MongoDB."""

    async def insert(self, funding: FundingRate) -> bool:
        """
        Insert a single funding rate.

        Args:
            funding: FundingRate model instance

        Returns:
            True if successful, False otherwise
        """
        try:
            collection = f"funding_rates_{funding.symbol}"
            count = await self.mongodb.write([funding], collection)
            self._persist_mysql([funding])
            return count > 0
        except Exception as e:
            logger.error(f"Failed to insert funding rate for {funding.symbol}: {e}")
            return False

    async def insert_batch(self, funding_rates: list[FundingRate]) -> int:
        """
        Insert multiple funding rates.

        Args:
            funding_rates: List of FundingRate model instances

        Returns:
            Number of funding rates successfully inserted
        """
        if not funding_rates:
            return 0

        try:
            # Group by symbol
            rates_by_symbol = {}
            for rate in funding_rates:
                if rate.symbol not in rates_by_symbol:
                    rates_by_symbol[rate.symbol] = []
                rates_by_symbol[rate.symbol].append(rate)

            # Insert each symbol's rates to its collection
            total_inserted = 0
            for symbol, symbol_rates in rates_by_symbol.items():
                collection = f"funding_rates_{symbol}"
                count = await self.mongodb.write(symbol_rates, collection)
                total_inserted += count
                self._persist_mysql(symbol_rates)
                logger.debug(f"Inserted {count} funding rates for {symbol}")

            return total_inserted

        except Exception as e:
            logger.error(f"Failed to insert funding rate batch: {e}")
            return 0

    def _persist_mysql(self, rates: list[FundingRate]) -> None:
        """Best-effort durable copy; MongoDB remains the operational path."""
        if (
            os.getenv("PETROSA_FUNDING_RATES_MYSQL_PERSIST_ENABLED", "true").lower()
            != "true"
            or self.mysql is None
        ):
            return
        try:
            rows = [
                FundingRateMySQLRow(
                    id=f"{rate.symbol}:{rate.timestamp.isoformat()}",
                    symbol=rate.symbol,
                    timestamp=rate.timestamp,
                    funding_rate=rate.funding_rate,
                    funding_time=rate.next_funding_time or rate.timestamp,
                    mark_price=rate.mark_price,
                    extracted_at=datetime.now(UTC),
                )
                for rate in rates
            ]
            self.mysql.write(rows, "funding_rates")
        except Exception:
            try:
                from data_manager.api.middleware.metrics import MYSQL_PERSIST_FAILURES

                MYSQL_PERSIST_FAILURES.labels(collection="funding_rates").inc()
            except Exception:
                logger.debug("Unable to record funding MySQL failure", exc_info=True)
            logger.warning("funding_rates_mysql_persist_failed", exc_info=True)

    async def get_range(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict]:
        """
        Get funding rates within time range.

        Args:
            symbol: Trading pair symbol
            start: Start datetime
            end: End datetime

        Returns:
            List of funding rate dictionaries
        """
        try:
            collection = f"funding_rates_{symbol}"
            return await self.mongodb.query_range(collection, start, end, symbol)
        except Exception as e:
            logger.error(f"Failed to query funding rates for {symbol}: {e}")
            return []

    async def get_latest(self, symbol: str, limit: int = 1) -> list[dict]:
        """
        Get most recent funding rates.

        Args:
            symbol: Trading pair symbol
            limit: Maximum number of rates to return

        Returns:
            List of funding rate dictionaries
        """
        try:
            collection = f"funding_rates_{symbol}"
            return await self.mongodb.query_latest(collection, symbol, limit)
        except Exception as e:
            logger.error(f"Failed to query latest funding rates for {symbol}: {e}")
            return []
