"""Risk inputs: realized sigma, correlation and the equity curve from synthetic data (dm#538)."""

import math
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes import risk as risk_route
from data_manager.services import risk_inputs as ri

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
DAY0 = datetime(2026, 8, 1, tzinfo=UTC)


def _candles(
    returns, *, start=DAY0, step=timedelta(days=1), start_price=100.0, skip=()
):
    """Candles whose consecutive log returns are exactly ``returns``; ``skip`` drops candle indices."""
    price, rows = start_price, [(0, start_price)]
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append((i, price))
    return [
        {"timestamp": start + step * i, "close": p}
        for i, p in rows
        if i not in set(skip)
    ]


def _alternating(n, r):
    return [r if i % 2 == 0 else -r for i in range(n)]


def test_daily_sigma_matches_the_known_value():
    n, r = 40, 0.01
    candles = _candles(_alternating(n, r), start=NOW - timedelta(days=n))
    daily, returns = ri.daily_sigma(candles, window_days=60, now=NOW)
    # alternating +-r with n even: mean 0, sample std = r * sqrt(n / (n - 1))
    assert daily["sigma_daily"] == pytest.approx(r * math.sqrt(n / (n - 1)), rel=1e-9)
    assert daily["n_returns"] == n == len(returns)
    assert daily["sufficient"] is True
    assert daily["window_start"] is not None and daily["window_end"] is not None


def test_window_keeps_only_the_trailing_returns():
    candles = _candles(_alternating(60, 0.01), start=NOW - timedelta(days=60))
    daily, _ = ri.daily_sigma(candles, window_days=30, now=NOW)
    assert daily["n_returns"] == 30


def test_no_return_spans_a_gap():
    # 30 candles with every fifth one missing: only adjacent-day pairs give a return
    candles = _candles(
        _alternating(30, 0.02), start=NOW - timedelta(days=30), skip={5, 10, 15, 20, 25}
    )
    _, returns = ri.daily_sigma(candles, window_days=60, now=NOW)
    # 31 candles, 5 dropped: 26 candles in 6 runs, so 20 returns; none across a gap
    assert len(returns) == 20
    assert all(
        abs(abs(r) - 0.02) < 1e-9 for _, r in returns
    )  # a spanning return would be 0 or 0.04


def test_sparse_daily_candles_are_insufficient_and_carry_no_number_when_unusable():
    sparse = _candles(
        _alternating(30, 0.01),
        start=NOW - timedelta(days=30),
        skip=set(range(1, 30, 2)),
    )
    daily, returns = ri.daily_sigma(sparse, window_days=30, now=NOW)
    assert returns == []  # every other day missing: no consecutive pair at all
    assert daily["sigma_daily"] is None
    assert daily["sufficient"] is False
    short = _candles(_alternating(10, 0.01), start=NOW - timedelta(days=10))
    daily, _ = ri.daily_sigma(short, window_days=30, now=NOW)
    assert daily["n_returns"] == 10
    assert daily["sigma_daily"] is not None and daily["sufficient"] is False  # n < 20


def test_hourly_sigma_horizon_floor_and_daily_equivalent():
    hours = 24 * 20
    r = 0.002
    candles = _candles(
        _alternating(hours, r),
        start=NOW - timedelta(hours=hours),
        step=timedelta(hours=1),
    )
    hourly = ri.hourly_sigma(
        candles, window_days=14, floor_days=60, horizon_hours=4.0, now=NOW
    )
    assert hourly["sigma_1h"] == pytest.approx(r, rel=0.01)
    assert hourly["sigma_horizon"] == pytest.approx(hourly["sigma_1h"] * 2.0)
    assert hourly["sigma_daily_from_1h"] == pytest.approx(
        hourly["sigma_1h"] * math.sqrt(24)
    )
    assert hourly["sufficient"] is True


def test_hourly_sigma_floor_keeps_a_calm_fortnight_from_collapsing_the_estimate():
    calm, wild = 24 * 14, 24 * 30
    returns = _alternating(wild, 0.01) + _alternating(calm, 0.0005)
    candles = _candles(
        returns, start=NOW - timedelta(hours=len(returns)), step=timedelta(hours=1)
    )
    hourly = ri.hourly_sigma(
        candles, window_days=14, floor_days=60, horizon_hours=4.0, now=NOW
    )
    assert hourly["sigma_1h_window"] < 0.001  # the calm last fortnight alone
    assert (
        hourly["sigma_1h_floor_window"] > 0.005
    )  # the longer window saw the wild period
    assert hourly["sigma_1h"] == hourly["sigma_1h_floor_window"]  # the floor wins


def test_hourly_sigma_with_too_few_hours_is_insufficient():
    candles = _candles(
        _alternating(48, 0.002),
        start=NOW - timedelta(hours=48),
        step=timedelta(hours=1),
    )
    hourly = ri.hourly_sigma(
        candles, window_days=14, floor_days=60, horizon_hours=4, now=NOW
    )
    assert hourly["sufficient"] is False


# --- correlation ---------------------------------------------------------------------------------


def _corr_inputs(n=40):
    base = [0.01 if (i // 2) % 2 == 0 else -0.01 for i in range(n)]  # ++--++--
    other = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]  # +-+-+-+-
    start = NOW - timedelta(days=n)
    out = {}
    for name, rets in {
        "AAA": base,
        "BBB": [2 * x for x in base],  # correlation +1
        "CCC": [-x for x in base],  # correlation -1
        "DDD": other,  # orthogonal to the base pattern: 0
    }.items():
        _, series = ri.daily_sigma(_candles(rets, start=start), window_days=60, now=NOW)
        out[name] = series
    return out


def test_correlation_matrix_of_known_series():
    corr = ri.correlation_matrix(_corr_inputs())
    m = corr["matrix"]
    assert m["AAA"]["BBB"] == pytest.approx(1.0)
    assert m["AAA"]["CCC"] == pytest.approx(-1.0)
    assert m["AAA"]["DDD"] == pytest.approx(0.0, abs=1e-9)
    assert m["BBB"]["AAA"] == m["AAA"]["BBB"]  # symmetric
    assert m["AAA"]["AAA"] == 1.0
    assert corr["n_common"]["AAA"]["BBB"] == 40
    assert corr["sufficient"]["AAA"]["BBB"] is True


def test_correlation_uses_only_the_common_days_and_flags_few():
    series = _corr_inputs()
    series["EEE"] = series["AAA"][-10:]  # only ten days in common
    corr = ri.correlation_matrix(series)
    assert corr["n_common"]["AAA"]["EEE"] == 10
    assert corr["sufficient"]["AAA"]["EEE"] is False
    assert corr["matrix"]["AAA"]["EEE"] == pytest.approx(1.0)


def test_correlation_without_variance_or_points_is_none():
    assert ri.correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert ri.correlation([1.0, 2.0], [1.0, 2.0]) is None


# --- equity curve --------------------------------------------------------------------------------


def _wallet_rows(balances, transfers=None, *, start=date(2026, 8, 1)):
    transfers = transfers or {}
    return [
        {
            "day": start + timedelta(days=i),
            "wallet_balance": str(b),
            "transfer": str(transfers.get(i, 0)),
            "balance_as_of_ms": int(
                datetime.combine(
                    start + timedelta(days=i), datetime.min.time(), UTC
                ).timestamp()
                * 1000
            ),
        }
        for i, b in enumerate(balances)
    ]


def test_equity_curve_sigma_peak_and_transfer_adjustment():
    n = 40
    balances, w = [1000.0], 1000.0
    for i in range(n):
        w *= 1.01 if i % 2 == 0 else 0.99
        balances.append(w)
    now = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=n + 1)
    curve = ri.equity_curve(_wallet_rows(balances), window_days=60, now=now)
    assert curve["n_returns"] == n
    assert curve["sufficient"] is True
    expected = ri.sample_std([0.01 if i % 2 == 0 else -0.01 for i in range(n)])
    assert curve["sigma_daily"] == pytest.approx(expected, rel=0.01)
    assert curve["peak"] == max(balances)
    assert curve["peak_at"] is not None
    assert curve["wallet_balance"] == pytest.approx(balances[-1])

    # a deposit is not a return: +500 on day 10 changes the balance, not the return of that day
    deposit, w = [1000.0], 1000.0
    for i in range(n):
        w = w * (1.01 if i % 2 == 0 else 0.99) + (500.0 if i == 9 else 0.0)
        deposit.append(w)
    with_transfer = ri.equity_curve(
        _wallet_rows(deposit, transfers={10: 500.0}), window_days=60, now=now
    )
    without = ri.equity_curve(_wallet_rows(deposit), window_days=60, now=now)
    assert with_transfer["sigma_daily"] == pytest.approx(expected, rel=0.01)
    assert (
        without["sigma_daily"] > 2 * expected
    )  # unadjusted, the deposit looks like a +50% day


def test_equity_curve_with_gaps_and_few_days_is_insufficient():
    rows = _wallet_rows([1000, 1010, 1020, 1030, 1040])
    del rows[2]  # a missing day: no return across it
    curve = ri.equity_curve(rows, window_days=30, now=datetime(2026, 8, 20, tzinfo=UTC))
    assert curve["n_returns"] == 2
    assert curve["sufficient"] is False
    assert ri.equity_curve([], window_days=30, now=NOW)["peak"] is None


# --- the endpoint --------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    now = datetime.now(UTC)
    n = 40
    rets = {
        "BTCUSDT": _alternating(n, 0.01),
        "ETHUSDT": [2 * x for x in _alternating(n, 0.01)],
    }

    async def candles(symbol, timeframe, start, end, db_manager=None):
        if timeframe == "1d":
            series = _candles(
                rets[symbol],
                start=now.replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=n),
            )
            return [c for c in series if start <= c["timestamp"] <= end]
        hours = 24 * 20
        return _candles(
            _alternating(hours, 0.002),
            start=now.replace(minute=0, second=0, microsecond=0)
            - timedelta(hours=hours),
            step=timedelta(hours=1),
        )

    async def wallet(first, last, db_manager=None):
        return _wallet_rows(
            [1000.0, 1010.0, 1005.0], start=date.today() - timedelta(days=3)
        )

    async def stored_peak(db_manager=None):
        return {"peak": 1234.5, "peak_at": "2026-10-06T00:00:00+00:00"}

    monkeypatch.setattr(risk_route, "_load_candles", candles)
    monkeypatch.setattr(risk_route, "_load_wallet_rows", wallet)
    monkeypatch.setattr(risk_route, "_load_stored_peak", stored_peak)
    api_module.db_manager = MagicMock()
    try:
        yield TestClient(create_app())
    finally:
        api_module.db_manager = None


def test_endpoint_returns_every_requested_symbol_with_flags(client):
    response = client.get("/api/v1/risk/inputs?symbols=BTCUSDT,ETHUSDT&window_days=60")
    assert response.status_code == 200
    body = response.json()
    assert set(body["symbols"]) == {"BTCUSDT", "ETHUSDT"}
    btc = body["symbols"]["BTCUSDT"]
    assert btc["daily"]["n_returns"] >= 20 and btc["daily"]["sufficient"] is True
    assert btc["sigma_daily_best"]["source"] == "klines_1d"
    assert btc["hourly"]["sufficient"] is True
    assert btc["hourly"]["horizon_hours"] == 4.0
    assert body["correlation"]["matrix"]["BTCUSDT"]["ETHUSDT"] == pytest.approx(1.0)
    # the shape tradeengine's equity-peak tracker reads: equity.peak and equity.peak_at
    assert body["equity"]["peak"] == 1010.0
    assert body["equity"]["peak_at"] is not None
    assert body["equity"]["stored_peak"]["peak"] == 1234.5
    assert body["equity"]["sufficient"] is False  # three days of snapshots
    assert body["symbols_unavailable"] == []


def test_endpoint_falls_back_to_1h_sigma_when_daily_candles_are_insufficient(
    client, monkeypatch
):
    original = risk_route._load_candles

    async def sparse_daily(symbol, timeframe, start, end, db_manager=None):
        if timeframe == "1d":
            return []
        return await original(symbol, timeframe, start, end, db_manager)

    monkeypatch.setattr(risk_route, "_load_candles", sparse_daily)
    body = client.get("/api/v1/risk/inputs?symbols=BTCUSDT").json()
    btc = body["symbols"]["BTCUSDT"]
    assert btc["daily"]["sufficient"] is False and btc["daily"]["sigma_daily"] is None
    best = btc["sigma_daily_best"]
    assert best["source"] == "klines_1h" and best["sufficient"] is True
    assert best["value"] == pytest.approx(btc["hourly"]["sigma_1h"] * math.sqrt(24))


def test_endpoint_never_fills_in_a_number_when_nothing_suffices(client, monkeypatch):
    async def empty(symbol, timeframe, start, end, db_manager=None):
        return []

    monkeypatch.setattr(risk_route, "_load_candles", empty)
    body = client.get("/api/v1/risk/inputs?symbols=BTCUSDT").json()
    btc = body["symbols"]["BTCUSDT"]
    assert btc["sufficient"] is False
    assert btc["sigma_daily_best"] == {
        "value": None,
        "source": None,
        "sufficient": False,
    }
    assert btc["hourly"]["sigma_1h"] is None


def test_endpoint_reports_a_failed_candle_read_instead_of_inventing_data(
    client, monkeypatch
):
    async def boom(symbol, timeframe, start, end, db_manager=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(risk_route, "_load_candles", boom)
    body = client.get("/api/v1/risk/inputs?symbols=BTCUSDT").json()
    assert body["symbols"] == {}
    assert body["symbols_unavailable"] == ["BTCUSDT"]


def test_endpoint_wallet_failure_is_labelled_unavailable(client, monkeypatch):
    async def boom(first, last):
        raise RuntimeError("mysql down")

    monkeypatch.setattr(risk_route, "_load_wallet_rows", boom)
    body = client.get("/api/v1/risk/inputs?symbols=BTCUSDT").json()
    assert body["equity"]["available"] is False
    assert body["equity"]["sufficient"] is False
    assert "peak" not in body["equity"]


def test_endpoint_503_without_a_database():
    api_module.db_manager = None
    assert TestClient(create_app()).get("/api/v1/risk/inputs").status_code == 503


# --- the median holding time per strategy (rule 23's H) -------------------------------------------


def _fill(strategy, side, price, minutes, **extra):
    return {
        "event_type": "filled",
        "strategy_id": strategy,
        "symbol": "BTCUSDT",
        "side": side,
        "fill_qty": 1.0,
        "fill_price": price,
        "fill_time": datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=minutes),
        **extra,
    }


def _rounds(strategy, holds, start=0):
    rows, at = [], start
    for hold in holds:
        rows += [
            _fill(strategy, "buy", 100.0, at),
            _fill(strategy, "sell", 101.0, at + hold),
        ]
        at += hold + 5
    return rows


def test_holding_times_are_the_median_over_closed_rounds():
    fills = _rounds("a", [10, 30, 50]) + [_fill("b", "buy", 100.0, 0)]
    out = risk_route.strategy_holding_times(
        fills, datetime(2026, 10, 7, tzinfo=UTC), 30.0
    )
    assert out["a"]["median_holding_seconds"] == 30 * 60
    assert (out["a"]["n"], out["a"]["closed_rounds"], out["a"]["source"]) == (
        3,
        3,
        "round_book",
    )
    assert (
        out["b"]["median_holding_seconds"] is None
    )  # only an open round: no number is made up


def test_holding_times_read_legacy_exit_sides_and_hedge_legs():
    fills = [
        _fill("a", "buy", 100.0, 0, position_side="LONG"),
        _fill(
            "a",
            "LONG",
            101.0,
            20,
            reason="oco_exit_take_profit",
            close_reason="take_profit",
        ),
    ]
    out = risk_route.strategy_holding_times(
        fills, datetime(2026, 10, 7, tzinfo=UTC), 30.0
    )
    assert out["a"]["median_holding_seconds"] == 20 * 60


def test_holding_times_only_count_rounds_in_the_window():
    fills = _rounds("a", [10, 10])
    old = datetime(2026, 12, 1, tzinfo=UTC)  # long after the rounds closed
    out = risk_route.strategy_holding_times(fills, old, 30.0)
    assert out["a"]["median_holding_seconds"] is None and out["a"]["n"] == 0
    assert out["a"]["closed_rounds"] == 2


def test_the_endpoint_serves_the_key_tradeengine_reads(client, monkeypatch):
    async def fills(end, db_manager=None):
        return _rounds("iceberg_detector", [60, 120, 180])

    monkeypatch.setattr(risk_route, "_load_fills", fills)
    body = client.get(
        "/api/v1/risk/inputs?symbols=BTCUSDT&strategies_window_days=365"
    ).json()
    # te#745's derived stop floor: strategies.<id>.median_holding_seconds
    assert body["strategies"]["iceberg_detector"]["median_holding_seconds"] == 120 * 60
    assert body["strategies_error"] is None


def test_a_failed_fill_read_is_reported_not_invented(client, monkeypatch):
    async def boom(end, db_manager=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(risk_route, "_load_fills", boom)
    body = client.get("/api/v1/risk/inputs?symbols=BTCUSDT").json()
    assert body["strategies"] == {}
    assert body["strategies_error"] == "strategy fills not readable"
    assert (
        "equity" in body and "symbols" in body
    )  # the rest of the response is unaffected
