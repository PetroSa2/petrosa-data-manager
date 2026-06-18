"""
Analytics endpoints for computed metrics.
"""

import logging
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

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
                    .get("computed_at", datetime.now(UTC))
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
                "last_updated": datetime.now(UTC).isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching volatility metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/performance/{strategy_id}")
async def get_strategy_performance(strategy_id: str):
    """
    Returns historical performance metrics for a specific strategy.

    Computes a real win-rate + recent-P&L trend by replaying the
    persisted `execution_events` fills through the FIFO P&L calculator
    (P4.1, #601). When the database is unavailable or no fills exist,
    the response degrades to a "no-data" payload rather than failing
    the request.
    """
    try:
        import data_manager.api.app as api_module
        from data_manager.services.pnl_calculator import PnlCalculator

        if not api_module.db_manager or not getattr(
            api_module.db_manager, "mongodb_adapter", None
        ):
            return {
                "stats": {
                    "win_rate": None,
                    "win_rate_delta": None,
                    "consecutive_losses": None,
                    "recent_pnl_trend": "unknown",
                },
                "metadata": {
                    "strategy_id": strategy_id,
                    "calculated_at": datetime.now(UTC).isoformat(),
                    "source": "data-manager-analysis-no-db",
                },
            }

        mongodb = api_module.db_manager.mongodb_adapter
        try:
            cursor = (
                mongodb.db["execution_events"]
                .find(
                    {
                        "strategy_id": strategy_id,
                        "event_type": {"$in": ["filled", "partial_fill"]},
                    }
                )
                .sort("timestamp", 1)
            )
            rows = await cursor.to_list(length=None)
        except Exception as exc:
            # A misconfigured / mock adapter (or a transient MongoDB
            # error) degrades to the no-data payload rather than 500ing.
            # Real errors still get logged for ops to investigate.
            logger.warning(
                "performance: execution_events read failed for %s: %s",
                strategy_id,
                exc,
            )
            return {
                "stats": {
                    "win_rate": None,
                    "win_rate_delta": None,
                    "consecutive_losses": None,
                    "recent_pnl_trend": "unknown",
                },
                "metadata": {
                    "strategy_id": strategy_id,
                    "calculated_at": datetime.now(UTC).isoformat(),
                    "source": "data-manager-analysis-no-db",
                },
            }

        calc = PnlCalculator()
        wins = 0
        losses = 0
        for row in rows:
            impact = calc.apply_fill(row)
            if impact is None or impact.realized_pnl == 0:
                continue
            if impact.realized_pnl > 0:
                wins += 1
            else:
                losses += 1

        decisions = wins + losses
        win_rate = (wins / decisions) if decisions else None
        breakdown = calc.strategy_pnl(strategy_id)
        recent_trend = (
            "positive"
            if breakdown.total > 0
            else "negative"
            if breakdown.total < 0
            else "flat"
        )

        return {
            "stats": {
                "win_rate": win_rate,
                "win_rate_delta": None,
                "consecutive_losses": None,
                "recent_pnl_trend": recent_trend,
                "realized_pnl": breakdown.realized,
                "unrealized_pnl": breakdown.unrealized,
            },
            "metadata": {
                "strategy_id": strategy_id,
                "calculated_at": datetime.now(UTC).isoformat(),
                "source": "data-manager-pnl-calculator",
                "fills_replayed": len(rows),
            },
        }
    except Exception as e:
        logger.error(
            f"Error getting strategy performance for {strategy_id}: {e}", exc_info=True
        )
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
                    .get("computed_at", datetime.now(UTC))
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
                "last_updated": datetime.now(UTC).isoformat(),
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
                    "timestamp": datetime.now(UTC).isoformat(),
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
                    .get("computed_at", datetime.now(UTC))
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
                    .get("computed_at", datetime.now(UTC))
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
                "last_updated": datetime.now(UTC).isoformat(),
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
                "last_updated": datetime.now(UTC).isoformat(),
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
            raise HTTPException(
                status_code=404, detail=f"No deviation data available for {pair}"
            )

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
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except HTTPException:
        raise
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
            raise HTTPException(
                status_code=404, detail=f"No seasonality data available for {pair}"
            )

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
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except HTTPException:
        raise
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

    Query params:
      - pair: trading pair symbol (e.g. "BTCUSDT")
      - period: data period hint (e.g. "1h", "1d") — stored in the analytics collection name

    Response shape (200 OK):
      {pair, metric="regime", data: {regime, volatility_level, volume_level,
       trend_direction, confidence} | null, metadata: {timestamp, collection}}

    When no regime has been computed yet for the pair, `data` is null and the
    status is still 200. Callers must treat null `data` as "regime unknown" —
    NOT as a wiring error. A 404 from this endpoint always means the route
    itself is missing, never that data is absent.

    503 → MongoDB adapter unavailable.
    500 → Unexpected server error.
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
                "metadata": {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "collection": collection,
                },
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
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except HTTPException:
        raise
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
                        key=lambda x: (
                            float(x[1]["volatility"]["annualized"]) if x[1] else 0
                        ),
                        reverse=(sort_order == "desc"),
                    )
                elif sort_by == "volume":
                    overview_list.sort(
                        key=lambda x: (
                            float(x[1]["volume"]["spike_ratio"]) if x[1] else 0
                        ),
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
            "timestamp": datetime.now(UTC).isoformat(),
            "pairs_requested": len(paginated_pair_list),
            "pairs_available": len([v for v in overview.values() if v is not None]),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating market overview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
