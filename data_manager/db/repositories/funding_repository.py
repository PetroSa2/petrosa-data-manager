"""
Repository for funding rate data operations.
"""

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

from prometheus_client import Counter
from pydantic import BaseModel

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import FundingRate

logger = logging.getLogger(__name__)

FUNDING_LEGACY_READS = Counter(
    "data_manager_funding_legacy_read_total",
    "Funding reads served from legacy per-symbol collections",
    ["symbol"],
)


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
            count = await self.mongodb.write([funding], "funding_rates")
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
            count = await self.mongodb.write(funding_rates, "funding_rates")
            self._persist_mysql(funding_rates)
            return count

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
            rates = await self.mongodb.query_range("funding_rates", start, end, symbol)
            if rates:
                return rates
            legacy = f"funding_rates_{symbol}"
            rates = await self.mongodb.query_range(legacy, start, end, symbol)
            if rates:
                FUNDING_LEGACY_READS.labels(symbol=symbol).inc()
            return rates
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
            rates = await self.mongodb.query_latest("funding_rates", symbol, limit)
            if rates:
                return rates
            legacy = f"funding_rates_{symbol}"
            rates = await self.mongodb.query_latest(legacy, symbol, limit)
            if rates:
                FUNDING_LEGACY_READS.labels(symbol=symbol).inc()
            return rates
        except Exception as e:
            logger.error(f"Failed to query latest funding rates for {symbol}: {e}")
            return []

    async def find_paginated(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
        descending: bool,
    ) -> tuple[list[dict], int]:
        """Return one canonical page, falling back to the legacy collection."""
        result = await self.mongodb.find_paginated(
            collection="funding_rates",
            filter_dict={"symbol": symbol},
            start=start,
            end=end,
            sort_list=[("timestamp", -1 if descending else 1)],
            limit=limit,
            offset=offset,
        )
        if result[1] > 0:
            return result
        legacy = f"funding_rates_{symbol}"
        result = await self.mongodb.find_paginated(
            collection=legacy,
            filter_dict={"symbol": symbol},
            start=start,
            end=end,
            sort_list=[("timestamp", -1 if descending else 1)],
            limit=limit,
            offset=offset,
        )
        if result[1] > 0:
            FUNDING_LEGACY_READS.labels(symbol=symbol).inc()
        return result

    async def ensure_indexes(self) -> None:
        """Ensure the canonical funding collection has its serving index."""
        await self.mongodb.ensure_indexes("funding_rates")
