"""
Data access endpoints for raw market data.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import data_manager.api.app as api_module
from data_manager.db.repositories import (
    CandleRepository,
    DepthRepository,
    FundingRepository,
    TradeRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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
            end = datetime.utcnow()
        if not start:
            start = end - timedelta(hours=24)

        # Query MongoDB
        candles = await candle_repo.get_range(pair, period, start, end)

        # Apply sorting
        if sort_order.lower() == "desc":
            candles = list(reversed(candles))

        total_count = len(candles)

        # Apply pagination
        paginated_candles = candles[offset : offset + limit]

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
                "data_completeness": 100.0,
                "last_updated": datetime.utcnow().isoformat(),
                "source": "mongodb",
                "collection": f"candles_{pair}_{period}",
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
            end = datetime.utcnow()
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
                "last_updated": datetime.utcnow().isoformat(),
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
                    "timestamp": datetime.utcnow().isoformat(),
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
            end = datetime.utcnow()
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
                "last_updated": datetime.utcnow().isoformat(),
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
