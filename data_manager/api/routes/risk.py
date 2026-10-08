"""``GET /api/v1/risk/inputs``: one read of the risk inputs (petrosa-data-manager#538).

Realized volatility and correlation from the candles, and the equity curve from the ledger's wallet-balance
snapshots, for the rules that need them (drawdown steps, cluster caps, the probation budget, the derived stop
floor). Reporting only: an item without enough returns is ``sufficient: false`` and carries no substitute.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import constants
import data_manager.api.app as api_module
from data_manager.services import risk_inputs as ri

logger = logging.getLogger(__name__)

router = APIRouter()

#: Where tradeengine keeps its own, higher-frequency equity peak (tradeengine#736), when it has written one.
STORED_PEAK_COLLECTION = "risk_equity_peak"


async def _load_candles(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    from data_manager.db.repositories import CandleRepository

    manager = api_module.db_manager
    repo = CandleRepository(manager.mysql_adapter, manager.mongodb_adapter)
    return await repo.get_range(symbol, timeframe, start, end)


async def _load_wallet_rows(first: datetime, last: datetime) -> list[dict[str, Any]]:
    from data_manager.db.repositories.ledger_repository import LedgerRepository

    manager = api_module.db_manager
    repo = LedgerRepository(manager.mysql_adapter, None)
    return await asyncio.to_thread(repo.wallet_series, first.date(), last.date())


async def _load_stored_peak() -> dict[str, Any] | None:
    manager = api_module.db_manager
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


@router.get("/inputs")
async def get_risk_inputs(
    window_days: int = Query(ri.DEFAULT_WINDOW_DAYS, ge=5, le=365),
    sigma_1h_days: int = Query(ri.DEFAULT_SIGMA_1H_DAYS, ge=2, le=90),
    sigma_1h_floor_days: int = Query(ri.DEFAULT_SIGMA_1H_FLOOR_DAYS, ge=2, le=365),
    horizon_hours: float = Query(ri.DEFAULT_HORIZON_HOURS, gt=0, le=24 * 30),
    symbols: str | None = Query(
        None, description="Comma-separated; default: the supported pairs"
    ),
) -> dict[str, Any]:
    """Realized sigma (daily, 1h and at a horizon), daily-return correlation and the equity curve."""
    if not api_module.db_manager:
        raise HTTPException(status_code=503, detail="Database not available")
    pairs = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else list(constants.SUPPORTED_PAIRS)
    )
    now = datetime.now(UTC)
    daily_start = now - timedelta(days=window_days + 2)
    hourly_start = now - timedelta(days=max(sigma_1h_days, sigma_1h_floor_days) + 1)

    async def one(symbol: str):
        daily_candles, hourly_candles = await asyncio.gather(
            _load_candles(symbol, "1d", daily_start, now),
            _load_candles(symbol, "1h", hourly_start, now),
        )
        return symbol, daily_candles, hourly_candles

    loaded = await asyncio.gather(*(one(s) for s in pairs), return_exceptions=True)
    per_symbol: dict[str, Any] = {}
    returns_by_symbol: dict[str, list] = {}
    for item in loaded:
        if isinstance(item, BaseException):
            logger.error("risk inputs: candle read failed: %s", item, exc_info=item)
            continue
        symbol, daily_candles, hourly_candles = item
        daily, returns = ri.daily_sigma(daily_candles, window_days=window_days, now=now)
        hourly = ri.hourly_sigma(
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
        rows = await _load_wallet_rows(now - timedelta(days=window_days + 400), now)
        equity = ri.equity_curve(rows, window_days=window_days, now=now)
    except Exception as exc:
        logger.error("risk inputs: wallet balance read failed: %s", exc, exc_info=True)
        equity = {
            "available": False,
            "sufficient": False,
            "error": "wallet balance read failed",
        }
    try:
        equity["stored_peak"] = await _load_stored_peak()
    except Exception as exc:
        logger.warning("risk inputs: stored equity peak not readable: %s", exc)
        equity["stored_peak"] = None

    return {
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
        "correlation": ri.correlation_matrix(returns_by_symbol),
        "equity": equity,
    }
