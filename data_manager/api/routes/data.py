"""
Data access endpoints for raw market data.
"""

import logging
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import constants
import data_manager.api.app as api_module
from data_manager.db.repositories import (
    CandleRepository,
    DepthRepository,
    FundingRepository,
    TradeRepository,
)
from data_manager.db.repositories.candle_repository import (
    mongo_collection_name,
    mysql_table_name,
)
from data_manager.maintenance.candle_readiness import evaluate_readiness
from data_manager.utils.time_utils import parse_timeframe_to_seconds

logger = logging.getLogger(__name__)

router = APIRouter()

# Fallback lookback when `period` cannot be parsed into an interval.
DEFAULT_CANDLE_WINDOW_HOURS = 24

# Extra intervals added to a derived lookback window so boundary alignment (a
# partially-closed newest candle) cannot silently shorten the returned window
# by one candle.
CANDLE_WINDOW_EDGE_INTERVALS = 2


def _default_candle_start(
    end: datetime, period: str, limit: int, offset: int
) -> datetime:
    """Derive the implicit start timestamp for a candle query.

    The previous behaviour hardcoded a 24-hour lookback regardless of
    ``period``, so a caller asking for 250 candles of ``1h`` could receive at
    most 24 and a caller asking for ``1d`` at most 1 — a *partial* window,
    which is exactly what #275 AC3 forbids on the execution path. Derive the
    window from what the caller actually asked for instead: ``offset + limit``
    candles' worth of time, plus a small edge margin.
    """
    try:
        interval_seconds = parse_timeframe_to_seconds(period)
    except (ValueError, TypeError):
        return end - timedelta(hours=DEFAULT_CANDLE_WINDOW_HOURS)

    candles_needed = max(1, offset + limit) + CANDLE_WINDOW_EDGE_INTERVALS
    return end - timedelta(seconds=interval_seconds * candles_needed)


def _completeness_pct(
    start: datetime, end: datetime, period: str, returned: int
) -> float:
    """Percentage of the requested window actually covered by data.

    Replaces the hardcoded ``100.0`` — which reported a perfectly complete
    dataset even when the query returned nothing — so #275 AC5 verification
    ("no gaps immediately after the flip") has a real signal to read.
    """
    try:
        interval_seconds = parse_timeframe_to_seconds(period)
    except (ValueError, TypeError):
        return 100.0 if returned else 0.0

    expected = int((end - start).total_seconds() // interval_seconds)
    if expected <= 0:
        return 100.0 if returned else 0.0
    return round(min(100.0, (returned / expected) * 100), 2)


class CandleResponse(BaseModel):
    """Candle data response."""

    pair: str
    period: str
    values: list[dict]
    metadata: dict
    parameters: dict


class TradeResponse(BaseModel):
    """Trade data response."""

    pair: str
    values: list[dict]
    metadata: dict
    parameters: dict


class DepthResponse(BaseModel):
    """Order book depth response."""

    pair: str
    data: dict
    metadata: dict
    parameters: dict


class FundingResponse(BaseModel):
    """Funding rate data response."""

    pair: str
    values: list[dict]
    metadata: dict
    parameters: dict


@router.get("/candles")
async def get_candles(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Candle period (e.g., '1m', '1h')"),
    start: datetime | None = Query(None, description="Start timestamp"),
    end: datetime | None = Query(None, description="End timestamp"),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of candles (default: 100, max: 1000)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_order: str = Query("asc", description="Sort order by timestamp (asc, desc)"),
) -> dict:
    """
    Get OHLCV candle data for a trading pair with pagination and sorting.

    Returns time series of candles with specified timeframe.
    Supports pagination via offset/limit and sorting by timestamp.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        # Initialize repository
        candle_repo = CandleRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        # Set default time range if not provided
        if not end:
            end = datetime.now(UTC)
        if not start:
            start = _default_candle_start(end, period, limit, offset)

        # Push the row cap, pagination offset and sort direction down into
        # the database query (petrosa-data-manager#331) instead of fetching
        # the full range, reversing it in Python and slicing the result.
        # ``ORDER BY`` direction must match ``sort_order`` *before* ``LIMIT``
        # is applied, or a DB-side cap would silently return the oldest N
        # candles for `sort_order="desc"` callers instead of the newest N.
        descending = sort_order.lower() == "desc"
        paginated_candles = await candle_repo.get_range(
            pair,
            period,
            start,
            end,
            limit=limit,
            offset=offset,
            descending=descending,
        )

        total_count = await candle_repo.count(pair, period, start, end)

        # Format response
        values = [
            {
                "timestamp": (
                    c.get("timestamp").isoformat()
                    if isinstance(c.get("timestamp"), datetime)
                    else str(c.get("timestamp"))
                ),
                "open": str(c.get("open")),
                "high": str(c.get("high")),
                "low": str(c.get("low")),
                "close": str(c.get("close")),
                "volume": str(c.get("volume")),
                "quote_volume": str(c.get("quote_volume"))
                if c.get("quote_volume")
                else None,
                "trades_count": c.get("trades_count"),
            }
            for c in paginated_candles
        ]

        return {
            "pair": pair,
            "period": period,
            "data": values,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
            },
            "sort": {
                "by": "timestamp",
                "order": sort_order,
            },
            "metadata": {
                "data_completeness": _completeness_pct(start, end, period, total_count),
                "last_updated": datetime.now(UTC).isoformat(),
                # Report the backend that actually answered instead of a
                # hardcoded "mongodb" (#275 AC5) — during the cutover the read
                # may have been served by the fallback backend.
                "source": candle_repo.last_read_source
                or constants.CANDLE_DATABASE_TYPE,
                "collection": (
                    mongo_collection_name(pair, period)
                    if candle_repo.last_read_source == "mongodb"
                    else mysql_table_name(period)
                ),
                "records_returned": len(values),
            },
            "parameters": {
                "pair": pair,
                "period": period,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching candles: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candles/readiness")
async def get_candle_readiness(
    pairs: str | None = Query(
        None, description="Comma-separated pairs (default: SUPPORTED_PAIRS)"
    ),
    timeframes: str | None = Query(
        None, description="Comma-separated timeframes (default: SUPPORTED_INTERVALS)"
    ),
    min_candles: int | None = Query(
        None, ge=1, description="Override CANDLE_WARMUP_MIN_CANDLES for this check"
    ),
) -> dict:
    """
    Mongo candle-store readiness gate (data-manager#275 AC2).

    Reports, per ``candles_{pair}_{timeframe}`` collection, whether MongoDB
    holds enough *and* fresh enough candles to become the primary execution
    candle store (#274 AC1). ``ready: false`` means the flip must not happen.

    The check is fail-closed: unreachable database, missing collection or an
    unparseable timeframe all resolve to not-ready. Always returns 200 — the
    verdict lives in the ``ready`` field so monitoring can scrape it without
    treating "not warm yet" as an HTTP error.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    def _split(raw: str | None) -> list[str] | None:
        if not raw:
            return None
        return [part.strip() for part in raw.split(",") if part.strip()]

    report = await evaluate_readiness(
        api_module.db_manager.mongodb_adapter,
        pairs=_split(pairs),
        timeframes=_split(timeframes),
        required_count=min_candles,
    )
    return report.to_dict()


@router.get("/trades")
async def get_trades(
    pair: str = Query(..., description="Trading pair symbol"),
    start: datetime | None = Query(None, description="Start timestamp"),
    end: datetime | None = Query(None, description="End timestamp"),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of trades (default: 100, max: 1000)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_order: str = Query("asc", description="Sort order by timestamp (asc, desc)"),
) -> dict:
    """
    Get individual trade data for a trading pair with pagination and sorting.

    Returns detailed trade execution history.
    Supports pagination via offset/limit and sorting by timestamp.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        trade_repo = TradeRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        # Set default time range
        if not end:
            end = datetime.now(UTC)
        if not start:
            start = end - timedelta(hours=1)

        trades = await trade_repo.get_range(pair, start, end)

        # Apply sorting
        if sort_order.lower() == "desc":
            trades = list(reversed(trades))

        total_count = len(trades)

        # Apply pagination
        paginated_trades = trades[offset : offset + limit]

        values = [
            {
                "timestamp": (
                    t.get("timestamp").isoformat()
                    if isinstance(t.get("timestamp"), datetime)
                    else str(t.get("timestamp"))
                ),
                "trade_id": t.get("trade_id"),
                "price": str(t.get("price")),
                "quantity": str(t.get("quantity")),
                "side": t.get("side"),
            }
            for t in paginated_trades
        ]

        return {
            "pair": pair,
            "data": values,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
            },
            "sort": {
                "by": "timestamp",
                "order": sort_order,
            },
            "metadata": {
                "data_completeness": 100.0,
                "last_updated": datetime.now(UTC).isoformat(),
                "source": "mongodb",
                "collection": f"trades_{pair}",
                "records_returned": len(values),
            },
            "parameters": {
                "pair": pair,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching trades: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/depth")
async def get_depth(
    pair: str = Query(..., description="Trading pair symbol"),
) -> DepthResponse:
    """
    Get current order book depth for a trading pair.

    Returns bid and ask levels with quantities.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        depth_repo = DepthRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        depth_data = await depth_repo.get_latest(pair, limit=1)

        if not depth_data:
            return DepthResponse(
                pair=pair,
                data={"bids": [], "asks": [], "last_update_id": 0},
                metadata={
                    "timestamp": datetime.now(UTC).isoformat(),
                    "source": "mongodb",
                },
                parameters={"pair": pair},
            )

        depth = depth_data[0]

        return DepthResponse(
            pair=pair,
            data={
                "bids": depth.get("bids", []),
                "asks": depth.get("asks", []),
                "last_update_id": depth.get("last_update_id", 0),
            },
            metadata={
                "timestamp": (
                    depth.get("timestamp").isoformat()
                    if isinstance(depth.get("timestamp"), datetime)
                    else str(depth.get("timestamp"))
                ),
                "source": "mongodb",
                "collection": f"depth_{pair}",
            },
            parameters={"pair": pair},
        )

    except Exception as e:
        logger.error(f"Error fetching depth: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/funding")
async def get_funding(
    pair: str = Query(..., description="Trading pair symbol"),
    start: datetime | None = Query(None, description="Start timestamp"),
    end: datetime | None = Query(None, description="End timestamp"),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of records (default: 100, max: 1000)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_order: str = Query("asc", description="Sort order by timestamp (asc, desc)"),
) -> dict:
    """
    Get funding rate data for a futures trading pair with pagination and sorting.

    Returns historical funding rates.
    Supports pagination via offset/limit and sorting by timestamp.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        funding_repo = FundingRepository(
            api_module.db_manager.mysql_adapter,
            api_module.db_manager.mongodb_adapter,
        )

        if not end:
            end = datetime.now(UTC)
        if not start:
            start = end - timedelta(days=7)

        funding_rates = await funding_repo.get_range(pair, start, end)

        # Apply sorting
        if sort_order.lower() == "desc":
            funding_rates = list(reversed(funding_rates))

        total_count = len(funding_rates)

        # Apply pagination
        paginated_funding_rates = funding_rates[offset : offset + limit]

        values = [
            {
                "timestamp": (
                    f.get("timestamp").isoformat()
                    if isinstance(f.get("timestamp"), datetime)
                    else str(f.get("timestamp"))
                ),
                "funding_rate": str(f.get("funding_rate")),
                "mark_price": str(f.get("mark_price")) if f.get("mark_price") else None,
            }
            for f in paginated_funding_rates
        ]

        return {
            "pair": pair,
            "data": values,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
            },
            "sort": {
                "by": "timestamp",
                "order": sort_order,
            },
            "metadata": {
                "data_completeness": 100.0,
                "last_updated": datetime.now(UTC).isoformat(),
                "source": "mongodb",
                "collection": f"funding_rates_{pair}",
                "records_returned": len(values),
            },
            "parameters": {
                "pair": pair,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching funding rates: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
