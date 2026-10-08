"""Risk inputs from one owner: realized volatility, correlation and the equity curve (petrosa-data-manager#538).

Recorded rules 5, 10, 11 and 23 on PetroSa2/petrosa_k8s#1239 need the same numbers: the realized daily sigma per
symbol, sigma on 1h candles and at a holding horizon, the correlation of daily returns, and the sigma and the peak
of the equity curve. This module only computes them, from candles and the ledger's wallet-balance snapshots; it
never fills in a missing number. An item without enough returns is reported with ``sufficient: false`` and no
value substitute, and the consumer uses its labelled fallback.

Returns are log returns across *consecutive* periods only: a candle after a gap contributes no return, so a
sparse series is never stretched across the gap.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from typing import Any

#: An item with fewer returns than this is reported as insufficient (daily returns).
MIN_DAILY_RETURNS = 20
#: 1h sigma needs at least a week of hourly returns.
MIN_HOURLY_RETURNS = 7 * 24
DEFAULT_WINDOW_DAYS = 30
DEFAULT_SIGMA_1H_DAYS = 14
DEFAULT_SIGMA_1H_FLOOR_DAYS = 60
DEFAULT_HORIZON_HOURS = 4.0


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        epoch = float(value)
        return datetime.fromtimestamp(epoch / 1000 if epoch > 1e12 else epoch, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and math.isfinite(number) else None


def closes(candles: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    """(open time, close) per candle, oldest first, one per time, unusable rows dropped."""
    seen: dict[datetime, float] = {}
    for candle in candles:
        when, close = _when(candle.get("timestamp")), _price(candle.get("close"))
        if when is not None and close is not None:
            seen[when] = close
    return sorted(seen.items())


def log_returns(
    series: list[tuple[datetime, float]], step: timedelta
) -> list[tuple[datetime, float]]:
    """Log returns between candles exactly ``step`` apart, stamped with the later candle's time."""
    out: list[tuple[datetime, float]] = []
    for (t0, p0), (t1, p1) in zip(series, series[1:], strict=False):
        if t1 - t0 == step:
            out.append((t1, math.log(p1 / p0)))
    return out


def sample_std(values: list[float]) -> float | None:
    """Sample standard deviation (n - 1); None below two values."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation of two equally long series; None below three points or without variance."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def daily_sigma(
    candles: list[dict[str, Any]], *, window_days: int, now: datetime
) -> tuple[dict[str, Any], list[tuple[datetime, float]]]:
    """Realized daily sigma over the trailing window, from consecutive daily closes."""
    returns = log_returns(closes(candles), timedelta(days=1))
    cutoff = now - timedelta(days=window_days)
    returns = [r for r in returns if r[0] > cutoff]
    sigma = sample_std([r for _, r in returns])
    return (
        {
            "sigma_daily": sigma,
            "n_returns": len(returns),
            "window_start": _iso(returns[0][0]) if returns else None,
            "window_end": _iso(returns[-1][0]) if returns else None,
            "sufficient": sigma is not None and len(returns) >= MIN_DAILY_RETURNS,
        },
        returns,
    )


def hourly_sigma(
    candles: list[dict[str, Any]],
    *,
    window_days: int,
    floor_days: int,
    horizon_hours: float,
    now: datetime,
) -> dict[str, Any]:
    """sigma_1h over the trailing window, floored by the longer window, and sigma at a horizon H.

    The floor is the larger of the short- and the long-window sigma, so a calm fortnight does not collapse the
    estimate. sigma_H = sigma_1h x sqrt(H); the daily sigma from 1h candles is sigma_1h x sqrt(24).
    """
    returns = log_returns(closes(candles), timedelta(hours=1))

    def window(days: int) -> list[float]:
        cutoff = now - timedelta(days=days)
        return [r for t, r in returns if t > cutoff]

    short, long_ = window(window_days), window(floor_days)
    sigma_short, sigma_long = sample_std(short), sample_std(long_)
    candidates = [s for s in (sigma_short, sigma_long) if s is not None]
    sigma = max(candidates) if candidates else None
    sufficient = sigma is not None and len(short) >= MIN_HOURLY_RETURNS
    return {
        "sigma_1h": sigma,
        "sigma_1h_window": sigma_short,
        "sigma_1h_floor_window": sigma_long,
        "n_returns": len(short),
        "n_returns_floor_window": len(long_),
        "horizon_hours": horizon_hours,
        "sigma_horizon": sigma * math.sqrt(horizon_hours)
        if sigma is not None
        else None,
        "sigma_daily_from_1h": sigma * math.sqrt(24.0) if sigma is not None else None,
        "sufficient": sufficient,
    }


def correlation_matrix(
    returns_by_symbol: dict[str, list[tuple[datetime, float]]],
) -> dict[str, Any]:
    """Pairwise correlation of daily returns over the days both symbols have, with ``n_common``."""
    symbols = sorted(returns_by_symbol)
    by_day = {s: dict(returns_by_symbol[s]) for s in symbols}
    matrix: dict[str, dict[str, float | None]] = {s: {} for s in symbols}
    n_common: dict[str, dict[str, int]] = {s: {} for s in symbols}
    sufficient: dict[str, dict[str, bool]] = {s: {} for s in symbols}
    for symbol in symbols:
        matrix[symbol][symbol] = 1.0 if by_day[symbol] else None
        n_common[symbol][symbol] = len(by_day[symbol])
        sufficient[symbol][symbol] = len(by_day[symbol]) >= MIN_DAILY_RETURNS
    for a, b in combinations(symbols, 2):
        days = sorted(set(by_day[a]) & set(by_day[b]))
        value = correlation([by_day[a][d] for d in days], [by_day[b][d] for d in days])
        for x, y in ((a, b), (b, a)):
            matrix[x][y] = value
            n_common[x][y] = len(days)
            sufficient[x][y] = value is not None and len(days) >= MIN_DAILY_RETURNS
    return {
        "symbols": symbols,
        "matrix": matrix,
        "n_common": n_common,
        "sufficient": sufficient,
    }


def equity_curve(
    rows: list[dict[str, Any]], *, window_days: int, now: datetime
) -> dict[str, Any]:
    """Sigma of the daily equity changes and the equity peak, from the ledger's wallet-balance snapshots.

    ``rows`` are the latest revision of each ledger day: ``day``, ``wallet_balance``, ``transfer`` (the day's
    deposits and withdrawals, taken out of the change so a deposit is not a return) and ``balance_as_of_ms``.
    The return of day d is (W_d - W_(d-1) - transfer_d) / W_(d-1), taken only between consecutive days.
    """
    points: list[tuple[date, float, float, Any]] = []
    for row in rows:
        day = row.get("day")
        if isinstance(day, datetime):
            day = day.date()
        try:
            balance = float(row["wallet_balance"])
            transfer = float(row.get("transfer") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(day, date) and math.isfinite(balance):
            points.append((day, balance, transfer, row.get("balance_as_of_ms")))
    points.sort(key=lambda p: p[0])
    returns: list[tuple[date, float]] = []
    for (d0, w0, _, _), (d1, w1, t1, _) in zip(points, points[1:], strict=False):
        if (d1 - d0).days == 1 and w0 > 0:
            returns.append((d1, (w1 - w0 - t1) / w0))
    cutoff = (now - timedelta(days=window_days)).date()
    windowed = [r for d, r in returns if d > cutoff]
    sigma = sample_std(windowed)
    peak = max(points, key=lambda p: p[1]) if points else None
    last = points[-1] if points else None
    peak_at = None
    if peak is not None:
        stamp = _when(peak[3])
        peak_at = _iso(stamp) if stamp is not None else peak[0].isoformat()
    return {
        "sigma_daily": sigma,
        "n_returns": len(windowed),
        "sufficient": sigma is not None and len(windowed) >= MIN_DAILY_RETURNS,
        "peak": peak[1] if peak else None,
        "peak_at": peak_at,
        "wallet_balance": last[1] if last else None,
        "wallet_balance_day": last[0].isoformat() if last else None,
        "days": len(points),
        "source": "ledger_exchange_day_revision",
    }
