"""Net R per closed round: the keep/kill input of the CIO (petrosa-data-manager#468)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes import strategy_net_r as route
from data_manager.services.round_book import RoundBook
from data_manager.services.strategy_net_r import (
    score_rounds,
    stop_fraction_of,
    strategy_net_r,
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


def test_net_r_is_net_over_entry_notional_times_the_stop():
    rows = _round_rows("A", 100, 110, 0, fee=0.1, qty=2, position_id="p1")
    scored, _ = _scored(rows, stops={"p1": D("0.02")})
    r = scored[0]
    assert r.entry_notional == D("200") and r.stop_fraction == D("0.02")
    assert r.net == D("19.8")  # 20 gross - 0.2 fees
    assert r.net_r == D("19.8") / D("4.00")  # risk = 200 x 2%


def test_a_round_without_a_known_stop_has_no_net_r():
    scored, _ = _scored(_round_rows("A", 100, 110, 0))
    assert scored[0].net_r is None and scored[0].stop_fraction is None


def test_the_stop_falls_back_to_the_decision():
    rows = _round_rows("A", 100, 110, 0, decision_id="d1")
    scored, _ = _scored(
        rows, decisions={"d1": {"source": "x", "stop_fraction": D("0.05")}}
    )
    assert scored[0].stop_fraction == D("0.05")


def test_the_position_stop_wins_over_the_decision_stop():
    rows = _round_rows("A", 100, 110, 0, position_id="p1", decision_id="d1")
    scored, _ = _scored(
        rows,
        stops={"p1": D("0.02")},
        decisions={"d1": {"stop_fraction": D("0.05")}},
    )
    assert scored[0].stop_fraction == D("0.02")


def test_unknown_fee_assets_are_counted_not_guessed():
    rows = _round_rows("A", 100, 110, 0, fee=0.1, asset="BNB")
    scored, _ = _scored(rows)
    assert scored[0].fees == D("0") and scored[0].fee_unknown_fills == 2


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
    assert [D(v) for v in out["A"]["net_r"]] == [D("1"), D("-0.5"), D("-1")]
    assert out["A"]["closed_rounds"] == 3  # oldest first, net / (100 x 10%)
    assert D(out["A"]["cumulative_net_loss_usd"]) == D("5")  # 10 - 5 - 10
    assert D(out["B"]["cumulative_net_loss_usd"]) == D("0")  # ahead: no loss
    assert [D(v) for v in out["B"]["net_r"]] == [D("2")]


def test_the_rounds_are_ordered_by_close_time():
    rows = _round_rows("A", 100, 110, 100, position_id="late") + _round_rows(
        "A", 100, 95, 0, position_id="early"
    )
    scored, _ = _scored(rows, stops={"late": D("0.1"), "early": D("0.1")})
    assert [r.net for r in scored] == [D("-5"), D("10")]


def test_legacy_exit_sides_and_hedge_legs_reach_the_net_r_input():
    rows = [
        _fill("A", "buy", 100, 0, fee=0.1, position_side="LONG"),
        _fill(
            "A",
            "LONG",
            110,
            5,
            fee=0.1,
            reason="oco_exit_take_profit",
            close_reason="take_profit",
        ),
        _fill("A", "sell", 100, 10, fee=0.1, position_side="SHORT"),
        _fill("A", "buy", 95, 15, fee=0.1, position_side="SHORT"),
    ]
    _, book = _scored(rows)
    assert [(r.position_side, r.realized_dec, r.fees) for r in book.closed] == [
        ("LONG", D("10"), D("0.2")),
        ("SHORT", D("5"), D("0.2")),
    ]


def test_a_round_that_flips_opens_the_next_one_with_the_flipped_notional():
    rows = [
        _fill("A", "buy", 100, 0, qty=1),
        _fill("A", "sell", 110, 5, qty=3),  # closes the long, opens a 2-lot short
        _fill("A", "buy", 105, 10, qty=2),  # closes the short
    ]
    _, book = _scored(rows)
    assert [r.entry_notional for r in book.closed] == [D("100"), D("220")]
    assert [r.realized_dec for r in book.closed] == [D("10"), D("10")]


# --- the route -----------------------------------------------------------------------------------


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
    api_module.db_manager = MagicMock()
    try:
        yield TestClient(create_app())
    finally:
        api_module.db_manager = None


@pytest.mark.parametrize("prefix", ["/analysis", "/api/v1/analysis"])
def test_strategy_net_r_serves_the_cio_contract_under_both_prefixes(client, prefix):
    body = client.get(f"{prefix}/strategy-net-r").json()
    a = body["strategies"]["A"]
    assert a["closed_rounds"] == 11
    assert [D(v) for v in a["net_r"]] == [
        D("5")
    ]  # only the round with a known stop: 10 / (100 x 2%)
    assert a["rounds_without_risk"] == 10
    assert D(a["cumulative_net_loss_usd"]) == D("0")


def test_the_drains_scorecard_routes_are_still_served_by_analysis(client):
    # the scorecard paths belong to scorecard_service in analysis.py: this module registers none of them
    paths = [route_.path for route_ in route.router.routes]
    assert paths == ["/strategy-net-r"]


def test_the_route_503s_without_a_database():
    api_module.db_manager = None
    assert TestClient(create_app()).get("/analysis/strategy-net-r").status_code == 503


def test_a_read_failure_is_a_500(monkeypatch):
    async def broken(end):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(route, "_load_fills", broken)
    api_module.db_manager = MagicMock()
    try:
        response = TestClient(create_app()).get("/analysis/strategy-net-r")
    finally:
        api_module.db_manager = None
    assert response.status_code == 500 and "mongo down" in response.text
