"""
Analytics endpoints for computed metrics.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


class MetricResponse(BaseModel):
    """Generic metric response."""

    pair: str
    period: str
    metric: str
    method: str
    window: str
    values: list[dict]
    metadata: dict


@router.get("/volatility")
async def get_volatility(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    method: str = Query("rolling_stddev", description="Volatility calculation method"),
    window: str = Query("30d", description="Time window for calculation"),
) -> MetricResponse:
    """
    Get volatility metrics for a trading pair.

    Supported methods: rolling_stddev, annualized, parkinson, garman_klass
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        # Query from MongoDB analytics collection
        collection = f"analytics_{pair}_volatility"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=10
        )

        # Format as time series
        values = [
            {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "rolling_stddev": str(r.get("rolling_stddev", "0")),
                "annualized": str(r.get("annualized_volatility", "0")),
                "parkinson": str(r.get("parkinson")) if r.get("parkinson") else None,
                "garman_klass": str(r.get("garman_klass"))
                if r.get("garman_klass")
                else None,
                "vov": (
                    str(r.get("volatility_of_volatility"))
                    if r.get("volatility_of_volatility")
                    else None
                ),
            }
            for r in results
        ]

        return MetricResponse(
            pair=pair,
            period=period,
            metric="volatility",
            method=method,
            window=window,
            values=values,
            metadata={
                "data_completeness": 100.0,
                "last_updated": datetime.utcnow().isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching volatility metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/volume")
async def get_volume(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    window: str = Query("24h", description="Time window for calculation"),
) -> MetricResponse:
    """
    Get volume metrics for a trading pair.

    Includes total volume, moving averages, delta, and spikes.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_volume"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=10
        )

        values = [
            {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "total_volume": str(r.get("total_volume", "0")),
                "volume_sma": str(r.get("volume_sma", "0")),
                "volume_ema": str(r.get("volume_ema", "0")),
                "volume_delta": str(r.get("volume_delta", "0")),
                "volume_spike_ratio": str(r.get("volume_spike_ratio", "1.0")),
            }
            for r in results
        ]

        return MetricResponse(
            pair=pair,
            period=period,
            metric="volume",
            method="aggregation",
            window=window,
            values=values,
            metadata={
                "data_completeness": 100.0,
                "last_updated": datetime.utcnow().isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching volume metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/spread")
async def get_spread(
    pair: str = Query(..., description="Trading pair symbol"),
) -> dict:
    """
    Get spread and liquidity metrics for a trading pair.

    Includes bid-ask spread, market depth, and liquidity ratio.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_spread"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            return {
                "pair": pair,
                "metric": "spread",
                "data": None,
                "metadata": {
                    "timestamp": datetime.utcnow().isoformat(),
                    "source": "mongodb",
                    "message": "No spread data available",
                },
            }

        r = results[0]

        return {
            "pair": pair,
            "metric": "spread",
            "data": {
                "bid_ask_spread": str(r.get("bid_ask_spread", "0")),
                "spread_percentage": str(r.get("spread_percentage", "0")),
                "market_depth_bid": str(r.get("market_depth_bid", "0")),
                "market_depth_ask": str(r.get("market_depth_ask", "0")),
                "liquidity_ratio": str(r.get("liquidity_ratio", "0")),
                "slippage_estimate": (
                    str(r.get("slippage_estimate"))
                    if r.get("slippage_estimate")
                    else None
                ),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "source": "mongodb",
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching spread metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trend")
async def get_trend(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    window: str = Query("20", description="Window size for moving averages"),
) -> MetricResponse:
    """
    Get trend and momentum indicators for a trading pair.

    Includes SMA, EMA, WMA, rate of change, and directional strength.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_trend"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=10
        )

        values = [
            {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "sma": str(r.get("sma", "0")),
                "ema": str(r.get("ema", "0")),
                "wma": str(r.get("wma", "0")),
                "rate_of_change": str(r.get("rate_of_change", "0")),
                "directional_strength": str(r.get("directional_strength", "50")),
                "crossover_signal": r.get("crossover_signal"),
            }
            for r in results
        ]

        return MetricResponse(
            pair=pair,
            period=period,
            metric="trend",
            method="moving_averages",
            window=window,
            values=values,
            metadata={
                "data_completeness": 100.0,
                "last_updated": datetime.utcnow().isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching trend metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/correlation")
async def get_correlation(
    pairs: str = Query(..., description="Comma-separated list of trading pairs"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    window: str = Query("30d", description="Time window for correlation"),
) -> dict:
    """
    Get correlation matrix for multiple trading pairs.

    Returns pairwise correlation coefficients.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        pair_list = [p.strip() for p in pairs.split(",")]

        # Query correlation matrix
        collection = "analytics_correlation_matrix"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, limit=1
        )

        if results and results[0].get("matrix"):
            correlation_matrix = results[0].get("matrix")
        else:
            correlation_matrix = {}

        return {
            "pairs": pair_list,
            "period": period,
            "metric": "correlation",
            "method": "pearson",
            "window": window,
            "correlation_matrix": correlation_matrix,
            "metadata": {
                "data_completeness": 100.0,
                "last_updated": datetime.utcnow().isoformat(),
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching correlation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/deviation")
async def get_deviation(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
) -> dict:
    """
    Get deviation and statistical metrics for a trading pair.

    Includes Bollinger Bands, Z-Score, autocorrelation.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_deviation"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            return {
                "pair": pair,
                "metric": "deviation",
                "data": None,
                "metadata": {"message": "No deviation data available"},
            }

        r = results[0]

        return {
            "pair": pair,
            "metric": "deviation",
            "data": {
                "standard_deviation": str(r.get("standard_deviation", "0")),
                "variance": str(r.get("variance", "0")),
                "z_score": str(r.get("z_score", "0")),
                "bollinger_upper": str(r.get("bollinger_upper", "0")),
                "bollinger_lower": str(r.get("bollinger_lower", "0")),
                "price_range_index": str(r.get("price_range_index", "0")),
                "autocorrelation": (
                    str(r.get("autocorrelation")) if r.get("autocorrelation") else None
                ),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching deviation metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/seasonality")
async def get_seasonality(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
) -> dict:
    """
    Get seasonality and cyclical patterns for a trading pair.

    Includes hourly/daily patterns, Fourier analysis, entropy.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_seasonality"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            return {
                "pair": pair,
                "metric": "seasonality",
                "data": None,
                "metadata": {"message": "No seasonality data available"},
            }

        r = results[0]

        return {
            "pair": pair,
            "metric": "seasonality",
            "data": {
                "hourly_pattern": {
                    k: str(v) for k, v in r.get("hourly_pattern", {}).items()
                },
                "daily_pattern": {
                    k: str(v) for k, v in r.get("daily_pattern", {}).items()
                },
                "seasonal_deviation": str(r.get("seasonal_deviation", "0")),
                "entropy_index": str(r.get("entropy_index", "0")),
                "dominant_cycle": r.get("dominant_cycle"),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching seasonality metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/regime")
async def get_regime(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query("1h", description="Data period (e.g., '1h', '1d')"),
) -> dict:
    """
    Get market regime classification for a trading pair.

    Returns current market regime and confidence level.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_regime"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            return {
                "pair": pair,
                "metric": "regime",
                "data": None,
                "metadata": {"message": "No regime data available"},
            }

        r = results[0]

        return {
            "pair": pair,
            "metric": "regime",
            "data": {
                "regime": r.get("regime", "unknown"),
                "volatility_level": r.get("volatility_level", "unknown"),
                "volume_level": r.get("volume_level", "unknown"),
                "trend_direction": r.get("trend_direction", "neutral"),
                "confidence": str(r.get("confidence", "0.5")),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.utcnow())
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching regime: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/market-overview")
async def market_overview(
    pairs: str = Query(
        "BTCUSDT,ETHUSDT", description="Comma-separated list of trading pairs"
    ),
    limit: int = Query(
        10,
        ge=1,
        le=100,
        description="Maximum number of pairs to return (default: 10, max: 100)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_by: str = Query(
        "symbol", description="Sort by field (symbol, volatility, volume, trend)"
    ),
    sort_order: str = Query("asc", description="Sort order (asc, desc)"),
) -> dict:
    """
    Get comprehensive market overview for multiple pairs with pagination.

    Returns volatility, volume, trend, and regime for each pair.
    Supports pagination and sorting for efficient data retrieval.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        pair_list = [p.strip() for p in pairs.split(",")]

        # Apply pagination to pair list
        total_pairs = len(pair_list)
        paginated_pair_list = pair_list[offset : offset + limit]

        overview = {}

        for pair in paginated_pair_list:
            try:
                # Get latest metrics for each type
                vol_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_volatility", symbol=pair, limit=1
                )
                volume_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_volume", symbol=pair, limit=1
                )
                trend_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_trend", symbol=pair, limit=1
                )
                regime_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_regime", symbol=pair, limit=1
                )

                overview[pair] = {
                    "volatility": {
                        "annualized": (
                            str(vol_data[0].get("annualized_volatility", "0"))
                            if vol_data
                            else "0"
                        ),
                    },
                    "volume": {
                        "spike_ratio": (
                            str(volume_data[0].get("volume_spike_ratio", "1.0"))
                            if volume_data
                            else "1.0"
                        ),
                    },
                    "trend": {
                        "direction": (
                            trend_data[0].get("crossover_signal", "neutral")
                            if trend_data
                            else "neutral"
                        ),
                        "roc": str(trend_data[0].get("rate_of_change", "0"))
                        if trend_data
                        else "0",
                    },
                    "regime": {
                        "classification": (
                            regime_data[0].get("regime", "unknown")
                            if regime_data
                            else "unknown"
                        ),
                        "confidence": (
                            str(regime_data[0].get("confidence", "0"))
                            if regime_data
                            else "0"
                        ),
                    },
                }

            except Exception as e:
                logger.warning(f"Error getting overview for {pair}: {e}")
                overview[pair] = None

        # Apply sorting if requested
        if sort_by != "symbol":
            try:
                # Convert overview dict to list of tuples for sorting
                overview_list = list(overview.items())
                if sort_by == "volatility":
                    overview_list.sort(
                        key=lambda x: float(x[1]["volatility"]["annualized"])
                        if x[1]
                        else 0,
                        reverse=(sort_order == "desc"),
                    )
                elif sort_by == "volume":
                    overview_list.sort(
                        key=lambda x: float(x[1]["volume"]["spike_ratio"])
                        if x[1]
                        else 0,
                        reverse=(sort_order == "desc"),
                    )
                elif sort_by == "trend":
                    overview_list.sort(
                        key=lambda x: float(x[1]["trend"]["roc"]) if x[1] else 0,
                        reverse=(sort_order == "desc"),
                    )
                overview = dict(overview_list)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Could not sort by {sort_by}: {e}")

        return {
            "overview": overview,
            "pagination": {
                "total": total_pairs,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1,
                "pages": (total_pairs + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_pairs,
                "has_previous": offset > 0,
            },
            "sort": {
                "by": sort_by,
                "order": sort_order,
            },
            "timestamp": datetime.utcnow().isoformat(),
            "pairs_requested": len(paginated_pair_list),
            "pairs_available": len([v for v in overview.values() if v is not None]),
        }

    except Exception as e:
        logger.error(f"Error generating market overview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
