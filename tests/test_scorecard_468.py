"""Cost-aware strategy scorecard on the round book (petrosa-data-manager#468)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes import scorecard as route
from data_manager.services.round_book import RoundBook
from data_manager.services.scorecard import (
    bootstrap_mean,
    evaluate,
    in_period,
    metrics,
    score_rounds,
    scorecard,
    stop_fraction_of,
    strategy_net_r,
    wilson,
)

D = Decimal
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _fill(
    strategy,
    side,
    price,
    minutes,
    *,
    qty=1,
    fee=None,
    asset="USDT",
    symbol="BTCUSDT",
    **extra,
):
    row = {
        "event_type": "filled",
        "strategy_id": strategy,
        "symbol": symbol,
        "side": side,
        "fill_qty": qty,
        "fill_price": price,
        "fill_time": T0 + timedelta(minutes=minutes),
        **extra,
    }
    if fee is not None:
        row.update(fee=fee, fee_asset=asset, fee_status="known")
    return row


def _round_rows(strategy, entry, exit_, start_minute, fee=None, **extra):
    return [
        _fill(strategy, "buy", entry, start_minute, fee=fee, **extra),
        _fill(strategy, "sell", exit_, start_minute + 5, fee=fee, **extra),
    ]


def _scored(rows, **kwargs):
    book = RoundBook()
    for row in rows:
        book.apply(row)
    return score_rounds(book.closed, **kwargs), book


def _ten_closes():
    rows = []
    for i in range(6):  # six wins of +10
        rows += _round_rows("A", 100, 110, i * 20, fee=0.1)
    for i in range(4):  # four losses of -5
        rows += _round_rows("A", 100, 95, (6 + i) * 20, fee=0.1)
    return rows


# --- the metrics ---------------------------------------------------------------------------------


def test_ten_closes_six_wins_four_losses_give_exact_win_rate_payoff_and_expectancy():
    scored, _ = _scored(_ten_closes())
    m = metrics(scored, minimum=5)
    assert m["n_trades"] == 10
    assert m["win_rate"] == "0.6"
    # net of two fee legs of 0.1: wins 9.8, losses -5.2
    assert D(m["payoff"]) == D("9.8") / D("5.2")
    assert m["expectancy_per_trade"] == "3.8"
    assert m["gross_pnl"] == "40" and m["fees"] == "2" and m["net_pnl"] == "38"
    assert m["avg_win"] == "9.8" and m["avg_loss"] == "-5.2"
    assert m["median_trade"] == "9.8"
    assert m["fee_share_of_gross"] == "0.05"
    assert m["sample_ok"] is True


def test_net_is_gross_minus_fees_on_every_group():
    scored, _ = _scored(_ten_closes())
    card = scorecard(scored, "strategy", 5)
    for group in card["groups"].values():
        assert D(group["net_pnl"]) == D(group["gross_pnl"]) - D(group["fees"]) - D(
            group["funding_allocated"]
        )
    assert card["total_net"] == "38" and card["total_fees"] == "2"


def test_expectancy_ex_top1_turns_negative_when_one_close_carries_the_period():
    rows = _round_rows("B", 100, 307.68, 0)  # +207.68
    for i in range(1, 8):
        rows += _round_rows("B", 100, 98, i * 20)  # -2 each, seven of them
    m = metrics(_scored(rows)[0], minimum=1)
    assert D(m["expectancy_per_trade"]) > 0  # (207.68 - 14) / 8
    assert D(m["expectancy_ex_top1"]) < 0  # -2: the single best close left out
    assert m["expectancy_ex_top1"] == "-2"


def test_max_drawdown_orders_trades_by_close_time():
    rows = []
    for i, exit_ in enumerate([110, 90, 90, 120]):  # +10, -10, -10, +20
        rows += _round_rows("A", 100, exit_, i * 20)
    m = metrics(_scored(rows)[0], minimum=1)
    assert m["max_drawdown"] == "20"  # peak 10, trough -10


def test_a_strategy_with_only_an_open_round_has_nulls_and_an_open_count():
    rows = [_fill("C", "buy", 100, 0)]
    scored, book = _scored(rows)
    card = scorecard(scored, "strategy", 3, {"C": 1})
    group = card["groups"]["C"]
    assert group["n_trades"] == 0 and group["win_rate"] is None
    assert group["expectancy_per_trade"] is None and group["payoff"] is None
    assert group["open_positions"] == 1
    assert book.open_rounds() == [("C", "BTCUSDT")]


def test_unknown_fee_assets_are_counted_not_guessed():
    rows = [
        _fill("A", "buy", 100, 0, fee=0.05, asset="BNB"),  # a fee in another asset
        _fill("A", "sell", 110, 5),  # no fee recorded
    ]
    m = metrics(_scored(rows)[0], minimum=1)
    assert m["fees"] == "0" and m["fee_unknown_fills"] == 2
    assert m["net_pnl"] == "10"


# --- periods, groups, modes ----------------------------------------------------------------------


def test_a_round_opened_on_one_day_and_closed_on_the_next_belongs_to_the_close_day():
    day = datetime(2026, 10, 1, 23, 50, tzinfo=UTC)
    rows = [
        {**_fill("A", "buy", 100, 0, fee=0.5), "fill_time": day},
        {
            **_fill("A", "sell", 109, 0, fee=0.5),
            "fill_time": day + timedelta(minutes=20),
        },  # 00:10 the 2nd
    ]
    scored, _ = _scored(rows)
    first, second = datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)
    assert in_period(scored, datetime(2026, 10, 1, tzinfo=UTC), first) == []
    selected = in_period(scored, first, second)
    assert len(selected) == 1
    assert selected[0].net == D("8")  # 9 gross - entry fee - exit fee


def test_group_by_strategy_symbol_and_cio_mode():
    rows = _round_rows("A", 100, 110, 0, symbol="BTCUSDT", decision_id="d1")
    rows += _round_rows("A", 100, 90, 20, symbol="ETHUSDT", decision_id="d2")
    rows += _round_rows(
        "A", 100, 105, 40, symbol="ETHUSDT", decision_id="d0"
    )  # before decisions existed
    decisions = {"d1": {"source": "cio_llm"}, "d2": {"source": "cio_bypass"}}
    scored, _ = _scored(rows, decisions=decisions)
    by_symbol = scorecard(scored, "strategy_symbol", 1)["groups"]
    assert (
        set(by_symbol) == {"A/BTCUSDT", "A/ETHUSDT"}
        and by_symbol["A/ETHUSDT"]["n_trades"] == 2
    )
    by_mode = scorecard(scored, "cio_mode", 1)["groups"]
    assert set(by_mode) == {"cio_llm", "cio_bypass", "unknown"}
    assert by_mode["unknown"]["gross_pnl"] == "5"


# --- risk and net R ------------------------------------------------------------------------------


def test_net_r_is_net_over_entry_notional_times_the_stop():
    rows = _round_rows("A", 100, 110, 0, fee=0.1, qty=2, position_id="p1")
    scored, _ = _scored(rows, stops={"p1": D("0.02")})
    r = scored[0]
    assert r.entry_notional == D("200") and r.stop_fraction == D("0.02")
    assert r.net == D("19.8")  # 20 gross - 0.2 fees
    assert r.net_r == D("19.8") / D("4.00")  # risk = 200 x 2%


def test_a_round_without_a_known_stop_has_no_net_r_and_is_counted():
    scored, _ = _scored(_round_rows("A", 100, 110, 0))
    assert scored[0].net_r is None
    m = metrics(scored, 1)
    assert m["net_r"] == {"n": 0, "mean": None, "rounds_without_risk": 1}


def test_the_stop_falls_back_to_the_decision():
    rows = _round_rows("A", 100, 110, 0, decision_id="d1")
    scored, _ = _scored(
        rows, decisions={"d1": {"source": "x", "stop_fraction": D("0.05")}}
    )
    assert scored[0].stop_fraction == D("0.05")


def test_stop_fraction_of_a_position_row():
    assert stop_fraction_of(100, 98) == D("0.02")
    assert stop_fraction_of(100, 103) == D("0.03")  # a short's stop above the entry
    assert stop_fraction_of(100, None) is None and stop_fraction_of(0, 5) is None
    assert stop_fraction_of(100, 100) is None


def test_the_keep_kill_contract():
    rows = []
    for i, exit_ in enumerate([110, 95, 90]):
        rows += _round_rows("A", 100, exit_, i * 20, position_id=f"p{i}")
    rows += _round_rows("B", 100, 120, 100, position_id="pb")
    stops = {"p0": D("0.1"), "p1": D("0.1"), "p2": D("0.1"), "pb": D("0.1")}
    scored, _ = _scored(rows, stops=stops)
    out = strategy_net_r(scored)["strategies"]
    assert out["A"]["net_r"] == ["1", "-0.5", "-1"]  # net / (100 x 10%), oldest first
    assert out["A"]["closed_rounds"] == 3
    assert out["A"]["cumulative_net_loss_usd"] == "5"  # 10 - 5 - 10
    assert out["B"]["cumulative_net_loss_usd"] == "0"  # ahead: no loss
    assert out["B"]["net_r"] == ["2"]


# --- intervals and evaluation --------------------------------------------------------------------


def test_wilson_interval_of_a_win_rate():
    interval = wilson(6, 10)
    assert interval["n"] == 10
    assert float(interval["low"]) == pytest.approx(0.3127, abs=1e-3)
    assert float(interval["high"]) == pytest.approx(0.8318, abs=1e-3)
    assert wilson(0, 0) == {"n": 0, "low": None, "high": None}


def test_the_bootstrap_interval_is_seeded_and_brackets_the_mean():
    values = [D(v) for v in (4, 6, 5, 7, 3, 5, 6, 4)]
    first, second = bootstrap_mean(values), bootstrap_mean(values)
    assert first == second
    assert D(first["low"]) < D("5") < D(first["high"])


def test_groups_below_the_minimum_report_sample_ok_false_with_their_intervals():
    m = metrics(_scored(_round_rows("A", 100, 110, 0))[0], minimum=10)
    assert m["sample_ok"] is False
    assert m["win_rate_interval"]["n"] == 1 and m["expectancy_interval"]["n"] == 1


def _card():
    rows = _ten_closes()
    for i in range(2):
        rows += _round_rows("W", 100, 80, 300 + i * 20)  # young and losing
    for i in range(6):
        rows += _round_rows(
            "L", 100, 99, 500 + i * 20
        )  # enough rounds, negative expectancy
    return scorecard(_scored(rows)[0], "strategy", 5)["groups"]


def test_evaluate_is_unconfigured_when_thresholds_are_unset():
    result = evaluate(
        _card(), min_trades=None, min_expectancy_net=D("0"), max_dd_fraction=D("0.5")
    )
    assert result["status"] == "unconfigured"
    assert (
        evaluate(
            _card(), min_trades=5, min_expectancy_net=None, max_dd_fraction=D("0.5")
        )["status"]
        == "unconfigured"
    )


def test_evaluate_keeps_watches_and_disables():
    result = evaluate(
        _card(),
        min_trades=5,
        min_expectancy_net=D("0"),
        max_dd_fraction=D("0.5"),
        capital=D("1000"),
    )
    groups = result["groups"]
    assert groups["A"]["status"] == "keep"
    assert groups["W"]["status"] == "watch"  # below the sample minimum, never disable
    assert groups["W"]["reason"] == "sample_below_minimum"
    assert (
        groups["L"]["status"] == "disable"
        and groups["L"]["reason"] == "expectancy_below_minimum"
    )


# --- the round book underneath -------------------------------------------------------------------


def test_legacy_exit_sides_and_hedge_legs_reach_the_scorecard():
    rows = [
        _fill("A", "buy", 100, 0, fee=0.1, position_side="LONG"),
        {
            **_fill(
                "A",
                "LONG",
                110,
                5,
                fee=0.1,
                reason="oco_exit_take_profit",
                close_reason="take_profit",
            )
        },
        _fill("A", "sell", 100, 10, fee=0.1, position_side="SHORT"),
        _fill("A", "buy", 95, 15, fee=0.1, position_side="SHORT"),
    ]
    scored, _ = _scored(rows)
    assert [(r.position_side, r.gross) for r in scored] == [
        ("LONG", D("10")),
        ("SHORT", D("5")),
    ]
    assert all(r.fees == D("0.2") for r in scored)


# --- the routes ----------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    rows = _ten_closes() + _round_rows(
        "A", 100, 110, 400, position_id="px", decision_id="dx"
    )

    async def fills(end):
        return list(rows)

    async def stops(ids):
        return {"px": D("0.02")} if "px" in ids else {}

    async def decisions(ids):
        return (
            {"dx": {"source": "cio_llm", "stop_fraction": None}} if "dx" in ids else {}
        )

    monkeypatch.setattr(route, "_load_fills", fills)
    monkeypatch.setattr(route, "_load_stops", stops)
    monkeypatch.setattr(route, "_load_decisions", decisions)
    for name in (
        "SCORECARD_MIN_TRADES",
        "SCORECARD_MIN_EXPECTANCY_NET",
        "SCORECARD_MAX_DD_FRACTION",
    ):
        monkeypatch.delenv(name, raising=False)
    api_module.db_manager = MagicMock()
    try:
        yield TestClient(create_app())
    finally:
        api_module.db_manager = None


@pytest.mark.parametrize("prefix", ["/analysis", "/api/v1/analysis"])
def test_the_scorecard_is_served_under_both_prefixes(client, prefix):
    body = client.get(f"{prefix}/scorecard?group_by=cio_mode").json()
    assert set(body["groups"]) == {"unknown", "cio_llm"}
    assert body["groups"]["cio_llm"]["n_trades"] == 1
    assert body["total_funding"] == "0"
    assert body["groups"]["unknown"]["sample_ok"] is None  # no minimum configured


def test_the_period_filters_by_close_time(client):
    cutoff = (T0 + timedelta(minutes=300)).isoformat()
    body = client.get("/analysis/scorecard", params={"from": cutoff}).json()
    assert body["groups"]["A"]["n_trades"] == 1


def test_strategy_net_r_serves_the_cio_contract(client):
    body = client.get("/api/v1/analysis/strategy-net-r").json()
    a = body["strategies"]["A"]
    assert a["closed_rounds"] == 11 and a["net_r"] == [
        "5"
    ]  # only the round with a known stop: 10 / (100 x 2%)
    assert a["rounds_without_risk"] == 10
    assert a["cumulative_net_loss_usd"] == "0"


def test_evaluate_route_is_unconfigured_then_configured(client, monkeypatch):
    assert client.get("/analysis/scorecard/evaluate").json()["status"] == "unconfigured"
    monkeypatch.setenv("SCORECARD_MIN_TRADES", "5")
    monkeypatch.setenv("SCORECARD_MIN_EXPECTANCY_NET", "0")
    monkeypatch.setenv("SCORECARD_MAX_DD_FRACTION", "0.5")
    result = client.get(
        "/analysis/scorecard/evaluate", params={"capital": "1000"}
    ).json()
    assert result["groups"]["A"]["status"] == "keep"


def test_routes_503_without_a_database():
    api_module.db_manager = None
    assert TestClient(create_app()).get("/analysis/scorecard").status_code == 503
