"""
Repository for candle/kline data operations.
"""

import logging
from datetime import datetime
from typing import Any

from prometheus_client import Counter

import constants
from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import Candle

logger = logging.getLogger(__name__)

# AC3 of data-manager#275 — observability for the cutover. Every increment is
# a read that the primary backend could not satisfy and the secondary did:
# during the Mongo warm-up this should trend to zero before the #274 flip, and
# stay at zero afterwards. A non-zero rate post-flip means the warm path is
# incomplete and the cutover should be rolled back.
CANDLE_READ_FALLBACKS = Counter(
    "data_manager_candle_read_fallbacks_total",
    "Candle reads served by the non-primary backend during cutover",
    ["primary", "operation"],
)

# Dual-write mirror outcomes (AC4 — rollback path). `outcome` is success or
# error; a rising error rate means a rollback to the mirrored backend would
# land on incomplete data.
CANDLE_DUAL_WRITES = Counter(
    "data_manager_candle_dual_writes_total",
    "Candle writes mirrored to the non-primary backend during cutover",
    ["mirror", "outcome"],
)

# MySQL klines columns -> canonical candle keys returned to API consumers.
_MYSQL_COLUMN_MAP = {
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "close": "close_price",
    "volume": "volume",
    "timestamp": "timestamp",
    "symbol": "symbol",
    "interval": "interval",
}


def mongo_collection_name(symbol: str, timeframe: str) -> str:
    """Return the Mongo collection holding candles for ``symbol``/``timeframe``."""
    return f"candles_{symbol}_{timeframe}"


def mysql_table_name(timeframe: str) -> str:
    """Map a timeframe to its MySQL klines table (``1h`` -> ``klines_h1``)."""
    if not timeframe:
        return "klines_unknown"

    unit = timeframe[-1]
    value = timeframe[:-1]
    return f"klines_{unit}{value}"


def map_mysql_row(row: dict[str, Any]) -> dict[str, Any]:
    """Rename MySQL klines columns onto the canonical candle keys."""
    return {key: row.get(column) for key, column in _MYSQL_COLUMN_MAP.items()}


class CandleRepository(BaseRepository):
    """
    Repository for managing candle data.
    Supports both MongoDB (collection per pair/timeframe) and
    MySQL (one table per timeframe with symbol column).

    During the #274 candle-store cutover the repository also provides the
    #275 AC3 safety net: when the primary backend returns an empty or short
    candle window, the read is retried against the other backend so execution
    never evaluates strategies on a starved window. Controlled by
    ``CANDLE_READ_FALLBACK_ENABLED`` (kill-switch) and, for writes,
    ``CANDLE_DUAL_WRITE_ENABLED`` (AC4 rollback safety).
    """

    #: Backend that served the most recent read ("mongodb" | "mysql" | None).
    #: Surfaced in the ``/data/candles`` response metadata so operators can see
    #: which store actually answered instead of a hardcoded label (#275 AC5).
    last_read_source: str | None = None

    def _get_collection_name(self, symbol: str, timeframe: str) -> str:
        """Get collection name for symbol and timeframe (MongoDB specific)."""
        return mongo_collection_name(symbol, timeframe)

    def _get_mysql_table_name(self, timeframe: str) -> str:
        """
        Map timeframe to MySQL table name.
        Converts '1h' to 'klines_h1', '15m' to 'klines_m15', etc.
        """
        return mysql_table_name(timeframe)

    # ------------------------------------------------------------------
    # Backend selection helpers (#275 AC3/AC4)
    # ------------------------------------------------------------------

    def _primary_is_mysql(self) -> bool:
        return constants.CANDLE_DATABASE_TYPE == "mysql"

    def _fallback_adapter(self) -> Any | None:
        """Return the non-primary adapter when a fallback read is permitted.

        ``None`` means no fallback: either the kill-switch is off or the
        secondary adapter was never wired up (the repository is routinely
        constructed with a single adapter).
        """
        if not constants.CANDLE_READ_FALLBACK_ENABLED:
            return None
        return self.mysql if self._primary_is_mysql() is False else self.mongodb

    async def _read_fallback_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Query the non-primary backend for a time range. Never raises."""
        adapter = self._fallback_adapter()
        if adapter is None:
            return []
        try:
            if self._primary_is_mysql():
                return await adapter.query_range(
                    self._get_collection_name(symbol, timeframe), start, end, symbol
                )
            rows = adapter.query_range(
                self._get_mysql_table_name(timeframe), start, end, symbol
            )
            return [map_mysql_row(row) for row in rows]
        except Exception as e:
            logger.warning(
                f"Candle fallback range read failed for {symbol} {timeframe}: {e}"
            )
            return []

    async def _read_fallback_latest(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[dict]:
        """Query the non-primary backend for the newest candles. Never raises."""
        adapter = self._fallback_adapter()
        if adapter is None:
            return []
        try:
            if self._primary_is_mysql():
                return await adapter.query_latest(
                    self._get_collection_name(symbol, timeframe), symbol, limit
                )
            rows = adapter.query_latest(
                self._get_mysql_table_name(timeframe), symbol, limit
            )
            return [map_mysql_row(row) for row in rows]
        except Exception as e:
            logger.warning(
                f"Candle fallback latest read failed for {symbol} {timeframe}: {e}"
            )
            return []

    def _record_fallback(self, operation: str, symbol: str, timeframe: str) -> None:
        primary = "mysql" if self._primary_is_mysql() else "mongodb"
        CANDLE_READ_FALLBACKS.labels(primary=primary, operation=operation).inc()
        logger.warning(
            "Candle %s for %s %s served by the non-primary backend "
            "(primary=%s returned an empty/short window) — cutover safety net "
            "engaged (data-manager#275 AC3)",
            operation,
            symbol,
            timeframe,
            primary,
        )

    async def _mirror_write(self, candles: list[Candle]) -> None:
        """Mirror a write onto the non-primary backend (AC4). Never raises."""
        if not constants.CANDLE_DUAL_WRITE_ENABLED or not candles:
            return

        mirror = "mongodb" if self._primary_is_mysql() else "mysql"
        adapter: Any = self.mongodb if self._primary_is_mysql() else self.mysql
        if adapter is None:
            return

        try:
            if self._primary_is_mysql():
                by_collection: dict[str, list[Candle]] = {}
                for candle in candles:
                    key = self._get_collection_name(candle.symbol, candle.timeframe)
                    by_collection.setdefault(key, []).append(candle)
                for collection, batch in by_collection.items():
                    await adapter.write(batch, collection)
            else:
                by_table: dict[str, list[Candle]] = {}
                for candle in candles:
                    key = self._get_mysql_table_name(candle.timeframe)
                    by_table.setdefault(key, []).append(candle)
                for table, batch in by_table.items():
                    adapter.write_batch(batch, table)
            CANDLE_DUAL_WRITES.labels(mirror=mirror, outcome="success").inc()
        except Exception as e:
            CANDLE_DUAL_WRITES.labels(mirror=mirror, outcome="error").inc()
            logger.warning(f"Candle dual-write mirror to {mirror} failed: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def insert(self, candle: Candle) -> bool:
        """
        Insert a single candle.

        Args:
            candle: Candle model instance

        Returns:
            True if successful, False otherwise
        """
        try:
            if self._primary_is_mysql():
                table = self._get_mysql_table_name(candle.timeframe)
                count = self.mysql.write([candle], table)
            else:
                collection = self._get_collection_name(candle.symbol, candle.timeframe)
                count = await self.mongodb.write([candle], collection)
            await self._mirror_write([candle])
            return count > 0
        except Exception as e:
            logger.error(
                f"Failed to insert candle for {candle.symbol} {candle.timeframe}: {e}"
            )
            return False

    async def insert_batch(self, candles: list[Candle]) -> int:
        """
        Insert multiple candles.

        Args:
            candles: List of Candle model instances

        Returns:
            Number of candles successfully inserted
        """
        if not candles:
            return 0

        try:
            total_inserted = 0
            if self._primary_is_mysql():
                # Group candles by timeframe
                candles_by_table: dict[str, list[Candle]] = {}
                for candle in candles:
                    table = self._get_mysql_table_name(candle.timeframe)
                    candles_by_table.setdefault(table, []).append(candle)

                for table, table_candles in candles_by_table.items():
                    count = self.mysql.write_batch(table_candles, table)
                    total_inserted += count
                    logger.debug(f"Inserted {count} candles to {table}")
            else:
                # Group candles by symbol and timeframe
                candles_by_collection: dict[str, list[Candle]] = {}
                for candle in candles:
                    collection = self._get_collection_name(
                        candle.symbol, candle.timeframe
                    )
                    candles_by_collection.setdefault(collection, []).append(candle)

                for collection, collection_candles in candles_by_collection.items():
                    count = await self.mongodb.write(collection_candles, collection)
                    total_inserted += count
                    logger.debug(f"Inserted {count} candles to {collection}")

            await self._mirror_write(candles)
            return total_inserted

        except Exception as e:
            logger.error(f"Failed to insert candle batch: {e}")
            return 0

    async def get_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict]:
        """
        Get candles within time range.

        Falls back to the non-primary backend when the primary returns nothing
        (#275 AC3) so execution never reads an empty window mid-cutover.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            start: Start datetime
            end: End datetime

        Returns:
            List of candle dictionaries
        """
        primary = "mysql" if self._primary_is_mysql() else "mongodb"
        candles: list[dict] = []
        try:
            if self._primary_is_mysql():
                table = self._get_mysql_table_name(timeframe)
                rows = self.mysql.query_range(table, start, end, symbol)
                candles = [map_mysql_row(row) for row in rows]
            else:
                collection = self._get_collection_name(symbol, timeframe)
                candles = await self.mongodb.query_range(collection, start, end, symbol)
        except Exception as e:
            logger.error(f"Failed to query candles for {symbol} {timeframe}: {e}")
            candles = []

        self.last_read_source = primary
        if candles:
            return candles

        fallback = await self._read_fallback_range(symbol, timeframe, start, end)
        if fallback:
            self._record_fallback("get_range", symbol, timeframe)
            self.last_read_source = "mongodb" if primary == "mysql" else "mysql"
            return fallback
        return candles

    async def get_latest(
        self, symbol: str, timeframe: str, limit: int = 1
    ) -> list[dict]:
        """
        Get most recent candles.

        A *short* result (fewer candles than requested) also triggers the
        cutover fallback (#275 AC3): a partially warm Mongo collection is as
        dangerous to strategy evaluation as an empty one. The longer of the two
        windows wins.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            limit: Maximum number of candles to return

        Returns:
            List of candle dictionaries
        """
        primary = "mysql" if self._primary_is_mysql() else "mongodb"
        candles: list[dict] = []
        try:
            if self._primary_is_mysql():
                table = self._get_mysql_table_name(timeframe)
                rows = self.mysql.query_latest(table, symbol, limit)
                candles = [map_mysql_row(row) for row in rows]
            else:
                collection = self._get_collection_name(symbol, timeframe)
                candles = await self.mongodb.query_latest(collection, symbol, limit)
        except Exception as e:
            logger.error(
                f"Failed to query latest candles for {symbol} {timeframe}: {e}"
            )
            candles = []

        self.last_read_source = primary
        if len(candles) >= limit:
            return candles

        fallback = await self._read_fallback_latest(symbol, timeframe, limit)
        if len(fallback) > len(candles):
            self._record_fallback("get_latest", symbol, timeframe)
            self.last_read_source = "mongodb" if primary == "mysql" else "mysql"
            return fallback
        return candles

    async def count(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """
        Count candles matching criteria.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            start: Optional start datetime
            end: Optional end datetime

        Returns:
            Number of matching candles
        """
        try:
            if self._primary_is_mysql():
                table = self._get_mysql_table_name(timeframe)
                return self.mysql.get_record_count(table, start, end, symbol)
            else:
                collection = self._get_collection_name(symbol, timeframe)
                return await self.mongodb.get_record_count(
                    collection, start, end, symbol
                )
        except Exception as e:
            logger.error(f"Failed to count candles for {symbol} {timeframe}: {e}")
            return 0

    async def ensure_indexes(self, symbol: str, timeframe: str) -> None:
        """
        Ensure indexes exist for collection/table.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
        """
        try:
            if self._primary_is_mysql():
                # MySQL indexes handled during table creation
                pass
            else:
                collection = self._get_collection_name(symbol, timeframe)
                await self.mongodb.ensure_indexes(collection)
        except Exception as e:
            logger.warning(f"Failed to ensure indexes for {symbol} {timeframe}: {e}")
