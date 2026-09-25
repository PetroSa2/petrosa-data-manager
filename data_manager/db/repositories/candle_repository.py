"""
Repository for candle/kline data operations.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from prometheus_client import Counter

import constants
from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.market_data import Candle, MySQLKlineRow
from data_manager.utils.time_utils import as_aware_utc, parse_timeframe_to_minutes

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
MYSQL_CANDLE_COLUMNS = tuple(_MYSQL_COLUMN_MAP.values())


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


def candle_to_mysql_kline(
    candle: Candle, *, extracted_at: datetime | None = None
) -> MySQLKlineRow:
    """Convert a canonical candle into a complete MySQL klines row."""
    open_time_aware = as_aware_utc(candle.timestamp)
    open_time = open_time_aware.replace(tzinfo=None)
    close_time = open_time + timedelta(
        minutes=parse_timeframe_to_minutes(candle.timeframe)
    )
    extracted_at_aware = as_aware_utc(extracted_at or datetime.now(UTC))
    price_change = candle.close - candle.open
    price_change_percent = (
        (price_change / candle.open * 100).quantize(Decimal("0.0001"))
        if candle.open != 0
        else Decimal("0")
    )

    return MySQLKlineRow(
        id=f"{candle.symbol}_{int(open_time_aware.timestamp() * 1000)}",
        symbol=candle.symbol,
        timestamp=open_time,
        open_time=open_time,
        close_time=close_time,
        interval=candle.timeframe,
        open_price=candle.open,
        high_price=candle.high,
        low_price=candle.low,
        close_price=candle.close,
        volume=candle.volume,
        quote_asset_volume=(
            candle.quote_volume if candle.quote_volume is not None else Decimal("0")
        ),
        number_of_trades=(
            candle.trades_count if candle.trades_count is not None else 0
        ),
        taker_buy_base_asset_volume=(
            candle.taker_buy_base_volume
            if candle.taker_buy_base_volume is not None
            else Decimal("0")
        ),
        taker_buy_quote_asset_volume=(
            candle.taker_buy_quote_volume
            if candle.taker_buy_quote_volume is not None
            else Decimal("0")
        ),
        price_change=price_change,
        price_change_percent=price_change_percent,
        extracted_at=extracted_at_aware.replace(tzinfo=None),
        extractor_version=constants.KLINE_WRITER_VERSION,
        source=constants.KLINE_WRITER_SOURCE,
    )


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
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        limit: int | None = None,
        offset: int = 0,
        descending: bool = False,
    ) -> list[dict]:
        """Query the non-primary backend for a time range. Never raises."""
        adapter = self._fallback_adapter()
        if adapter is None:
            return []
        try:
            if self._primary_is_mysql():
                return await adapter.query_range(
                    self._get_collection_name(symbol, timeframe),
                    start,
                    end,
                    symbol,
                    limit=limit,
                    offset=offset,
                    descending=descending,
                )
            rows = adapter.query_range(
                self._get_mysql_table_name(timeframe),
                start,
                end,
                symbol,
                limit=limit,
                offset=offset,
                descending=descending,
                columns=MYSQL_CANDLE_COLUMNS,
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
                self._get_mysql_table_name(timeframe),
                symbol,
                limit,
                columns=MYSQL_CANDLE_COLUMNS,
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
                    adapter.write_batch(
                        [candle_to_mysql_kline(candle) for candle in batch], table
                    )
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
                count = self.mysql.write([candle_to_mysql_kline(candle)], table)
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
                    # petrosa-data-manager#312: self.mysql.write_batch() is a
                    # synchronous SQLAlchemy call that blocks on network I/O.
                    # Calling it inline (unlike the mongodb branch below,
                    # which is genuinely async) freezes the whole asyncio
                    # event loop -- including the liveness/readiness HTTP
                    # handlers -- for the duration of the DB round-trip.
                    # Offload to a worker thread so the loop stays responsive.
                    count = await asyncio.to_thread(
                        self.mysql.write_batch,
                        [candle_to_mysql_kline(candle) for candle in table_candles],
                        table,
                    )
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
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        limit: int | None = None,
        offset: int = 0,
        descending: bool = False,
    ) -> list[dict]:
        """
        Get candles within time range.

        Falls back to the non-primary backend when the primary returns nothing
        (#275 AC3) so execution never reads an empty window mid-cutover.

        ``limit``/``offset``/``descending`` are pushed down to the adapter
        query itself (petrosa-data-manager#331) rather than the caller
        fetching the full range and slicing it in Python. ``descending``
        determines the DB-side ``ORDER BY`` direction *before* ``LIMIT`` is
        applied, so a caller asking for the newest N candles (``descending=
        True``) gets the newest N — not the oldest N reversed, which is what
        the previous Python-side ``reversed()`` + slice actually did once a
        row cap is introduced.

        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe (e.g., '1m', '1h')
            start: Start datetime
            end: End datetime
            limit: Maximum rows to return (``None`` = full range, unchanged
                behaviour for callers that need every row).
            offset: Rows to skip before ``limit`` is applied.
            descending: Sort newest-first when ``True`` (default oldest-first).

        Returns:
            List of candle dictionaries
        """
        primary = "mysql" if self._primary_is_mysql() else "mongodb"
        candles: list[dict] = []
        try:
            if self._primary_is_mysql():
                table = self._get_mysql_table_name(timeframe)
                # petrosa-data-manager#312: offload the blocking SQLAlchemy
                # call so it doesn't stall the event loop (see write_batch
                # comment above for the full rationale).
                rows = await asyncio.to_thread(
                    self.mysql.query_range,
                    table,
                    start,
                    end,
                    symbol,
                    limit=limit,
                    offset=offset,
                    descending=descending,
                    columns=MYSQL_CANDLE_COLUMNS,
                )
                candles = [map_mysql_row(row) for row in rows]
            else:
                collection = self._get_collection_name(symbol, timeframe)
                candles = await self.mongodb.query_range(
                    collection,
                    start,
                    end,
                    symbol,
                    limit=limit,
                    offset=offset,
                    descending=descending,
                )
        except Exception as e:
            logger.error(f"Failed to query candles for {symbol} {timeframe}: {e}")
            candles = []

        self.last_read_source = primary
        if candles:
            return candles

        fallback = await self._read_fallback_range(
            symbol,
            timeframe,
            start,
            end,
            limit=limit,
            offset=offset,
            descending=descending,
        )
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
                # petrosa-data-manager#312: see write_batch comment above.
                rows = await asyncio.to_thread(
                    self.mysql.query_latest,
                    table,
                    symbol,
                    limit,
                    columns=MYSQL_CANDLE_COLUMNS,
                )
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
                # petrosa-data-manager#312: see write_batch comment above.
                return await asyncio.to_thread(
                    self.mysql.get_record_count, table, start, end, symbol
                )
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
