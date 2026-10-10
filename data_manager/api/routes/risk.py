"""``GET /api/v1/risk/inputs``: one read of the risk inputs (petrosa-data-manager#538).

Realized volatility and correlation from the candles, and the equity curve from the ledger's wallet-balance
snapshots, for the rules that need them (drawdown steps, cluster caps, the probation budget, the derived stop
floor). Reporting only: an item without enough returns is ``sufficient: false`` and carries no substitute.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import constants
import data_manager.api.app as api_module
from data_manager.services import risk_inputs as ri
from data_manager.services.report_precompute import record_report_stage
from data_manager.services.round_book import FILL_EVENT_TYPES, RoundBook, _when

logger = logging.getLogger(__name__)

router = APIRouter()

#: Where tradeengine keeps its own, higher-frequency equity peak (tradeengine#736), when it has written one.
STORED_PEAK_COLLECTION = "risk_equity_peak"


async def _load_candles(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    db_manager: Any | None = None,
) -> list[dict[str, Any]]:
    from data_manager.db.repositories import CandleRepository

    manager = db_manager or api_module.db_manager
    repo = CandleRepository(manager.mysql_adapter, manager.mongodb_adapter)
    return await repo.get_range(symbol, timeframe, start, end)


async def _load_wallet_rows(
    first: datetime, last: datetime, db_manager: Any | None = None
) -> list[dict[str, Any]]:
    from data_manager.db.repositories.ledger_repository import LedgerRepository

    manager = db_manager or api_module.db_manager
    repo = LedgerRepository(manager.mysql_adapter, None)
    return await asyncio.to_thread(repo.wallet_series, first.date(), last.date())


_MAX_FILLS = 200_000


async def _load_fills(
    end: datetime, db_manager: Any | None = None
) -> list[dict[str, Any]]:
    manager = db_manager or api_module.db_manager
    mongodb = manager.mongodb_adapter
    query: dict[str, Any] = {
        "event_type": {"$in": sorted(FILL_EVENT_TYPES)},
        "timestamp": {"$lt": end},
    }
    return (
        await mongodb.db["execution_events"]
        .find(query)
        .sort("timestamp", 1)
        .to_list(length=_MAX_FILLS)
    )


def strategy_holding_times(
    fills: list[dict[str, Any]], now: datetime, window_days: float
) -> dict[str, Any]:
    """Per strategy: the median holding time of its closed rounds (from the round book, hedge legs and legacy
    exit sides handled), the rounds behind it (``n``) and the closed-round rate. The median is None without a
    closed round in the window: no number is made up."""
    book = RoundBook()
    for row in sorted(
        fills, key=lambda r: _when(r) or datetime.min.replace(tzinfo=UTC)
    ):
        book.apply(row)
    report = book.report(now=now, window_days=window_days)
    return {
        strategy_id: {
            "median_holding_seconds": stats["median_holding_seconds"],
            "n": stats["n"],
            "closed_rounds": stats["closed_rounds"],
            "closed_round_rate_per_day": stats["closed_round_rate_per_day"],
            "window_days": window_days,
            "source": "round_book",
        }
        for strategy_id, stats in report["strategies"].items()
    }


async def _load_stored_peak(db_manager: Any | None = None) -> dict[str, Any] | None:
    manager = db_manager or api_module.db_manager
    adapter = getattr(manager, "mongodb_adapter", None)
    if adapter is None:
        return None
    row = await adapter.db[STORED_PEAK_COLLECTION].find_one(
        {}, sort=[("equity_peak", -1)]
    )
    if not row:
        return None
    return {"peak": row.get("equity_peak"), "peak_at": row.get("peak_at")}


def _daily_best(daily: dict[str, Any], hourly: dict[str, Any]) -> dict[str, Any]:
    """The daily sigma to use: the daily candles when they suffice, else sigma_1h x sqrt(24), else none."""
    if daily["sufficient"]:
        return {
            "value": daily["sigma_daily"],
            "source": "klines_1d",
            "sufficient": True,
        }
    if hourly["sufficient"]:
        return {
            "value": hourly["sigma_daily_from_1h"],
            "source": "klines_1h",
            "sufficient": True,
        }
    return {"value": None, "source": None, "sufficient": False}


async def compute_risk_inputs(
    db_manager: Any,
    window_days: int = ri.DEFAULT_WINDOW_DAYS,
    sigma_1h_days: int = ri.DEFAULT_SIGMA_1H_DAYS,
    sigma_1h_floor_days: int = ri.DEFAULT_SIGMA_1H_FLOOR_DAYS,
    horizon_hours: float = ri.DEFAULT_HORIZON_HOURS,
    symbols: str | None = None,
    strategies_window_days: float = 30.0,
) -> dict[str, Any]:
    """Realized sigma (daily, 1h and at a horizon), daily-return correlation and the equity curve."""
    if not db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    pairs = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else list(constants.SUPPORTED_PAIRS)
    )
    now = datetime.now(UTC)
    daily_start = now - timedelta(days=window_days + 2)
    hourly_start = now - timedelta(days=max(sigma_1h_days, sigma_1h_floor_days) + 1)
    read_started = monotonic()

    async def one(symbol: str):
        daily_candles, hourly_candles = await asyncio.gather(
            _load_candles(symbol, "1d", daily_start, now, db_manager),
            _load_candles(symbol, "1h", hourly_start, now, db_manager),
        )
        return symbol, daily_candles, hourly_candles

    loaded = await asyncio.gather(*(one(s) for s in pairs), return_exceptions=True)
    record_report_stage("risk_inputs", "read", monotonic() - read_started)
    decode_started = monotonic()
    loaded = list(loaded)
    record_report_stage("risk_inputs", "decode", monotonic() - decode_started)
    compute_started = monotonic()
    per_symbol: dict[str, Any] = {}
    returns_by_symbol: dict[str, list] = {}
    for item in loaded:
        if isinstance(item, BaseException):
            logger.error("risk inputs: candle read failed: %s", item, exc_info=item)
            continue
        symbol, daily_candles, hourly_candles = item
        daily, returns = await asyncio.to_thread(
            ri.daily_sigma, daily_candles, window_days=window_days, now=now
        )
        hourly = await asyncio.to_thread(
            ri.hourly_sigma,
            hourly_candles,
            window_days=sigma_1h_days,
            floor_days=sigma_1h_floor_days,
            horizon_hours=horizon_hours,
            now=now,
        )
        returns_by_symbol[symbol] = returns
        per_symbol[symbol] = {
            "daily": daily,
            "hourly": hourly,
            "sigma_daily_best": _daily_best(daily, hourly),
            "sufficient": daily["sufficient"] or hourly["sufficient"],
        }
    missing = [s for s in pairs if s not in per_symbol]

    equity: dict[str, Any]
    try:
        rows = await _load_wallet_rows(
            now - timedelta(days=window_days + 400), now, db_manager
        )
        equity = await asyncio.to_thread(
            ri.equity_curve, rows, window_days=window_days, now=now
        )
    except Exception as exc:
        logger.error("risk inputs: wallet balance read failed: %s", exc, exc_info=True)
        equity = {
            "available": False,
            "sufficient": False,
            "error": "wallet balance read failed",
        }
    try:
        equity["stored_peak"] = await _load_stored_peak(db_manager)
    except Exception as exc:
        logger.warning("risk inputs: stored equity peak not readable: %s", exc)
        equity["stored_peak"] = None

    # The median holding time per strategy (rule 23's H, rule 7's integrity flag), from the round book
    strategies: dict[str, Any] = {}
    strategies_error: str | None = None
    try:
        strategies = await asyncio.to_thread(
            strategy_holding_times,
            await _load_fills(now, db_manager),
            now,
            strategies_window_days,
        )
    except Exception as exc:
        logger.error("risk inputs: strategy fills not readable: %s", exc, exc_info=True)
        strategies_error = "strategy fills not readable"

    report = {
        "as_of": now.isoformat(),
        "params": {
            "window_days": window_days,
            "sigma_1h_days": sigma_1h_days,
            "sigma_1h_floor_days": sigma_1h_floor_days,
            "horizon_hours": horizon_hours,
            "min_daily_returns": ri.MIN_DAILY_RETURNS,
            "min_hourly_returns": ri.MIN_HOURLY_RETURNS,
        },
        "symbols": per_symbol,
        "symbols_unavailable": missing,
        "correlation": await asyncio.to_thread(
            ri.correlation_matrix, returns_by_symbol
        ),
        "equity": equity,
        "strategies": strategies,
        "strategies_error": strategies_error,
    }
    record_report_stage("risk_inputs", "compute", monotonic() - compute_started)
    return report


@router.get("/inputs")
async def get_risk_inputs(
    window_days: int = Query(ri.DEFAULT_WINDOW_DAYS, ge=5, le=365),
    sigma_1h_days: int = Query(ri.DEFAULT_SIGMA_1H_DAYS, ge=2, le=90),
    sigma_1h_floor_days: int = Query(ri.DEFAULT_SIGMA_1H_FLOOR_DAYS, ge=2, le=365),
    horizon_hours: float = Query(ri.DEFAULT_HORIZON_HOURS, gt=0, le=24 * 30),
    symbols: str | None = Query(
        None, description="Comma-separated; default: the supported pairs"
    ),
    strategies_window_days: float = Query(30.0, gt=0, le=365),
) -> dict[str, Any]:
    precomputer = getattr(api_module, "report_precomputer", None)
    default_request = (
        symbols is None
        and window_days == ri.DEFAULT_WINDOW_DAYS
        and sigma_1h_days == ri.DEFAULT_SIGMA_1H_DAYS
        and sigma_1h_floor_days == ri.DEFAULT_SIGMA_1H_FLOOR_DAYS
        and horizon_hours == ri.DEFAULT_HORIZON_HOURS
        and strategies_window_days == 30
    )
    if precomputer is not None and default_request:
        cached = await precomputer.get_or_compute(
            "risk_inputs",
            lambda: compute_risk_inputs(api_module.db_manager, window_days=30),
            window_days=30,
        )
        if cached is not None:
            return cached
        raise HTTPException(
            status_code=503,
            detail="report_warming",
            headers={"Retry-After": "15"},
        )
    return await compute_risk_inputs(
        api_module.db_manager,
        window_days,
        sigma_1h_days,
        sigma_1h_floor_days,
        horizon_hours,
        symbols,
        strategies_window_days,
    )
