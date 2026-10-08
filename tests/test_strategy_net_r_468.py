"""Net R per closed round: the keep/kill input of the CIO (petrosa-data-manager#468)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

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


# --- the loaders against the audit collections -----------------------------------------------------


def _db(rows_by_collection):
    """A mongodb adapter whose ``db[collection].find(q).sort(..).to_list(..)`` returns the given rows."""
    seen = {}

    def collection(name):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.to_list = AsyncMock(return_value=list(rows_by_collection.get(name, [])))
        coll = MagicMock()

        def find(query):
            seen[name] = query
            return cursor

        coll.find.side_effect = find
        return coll

    adapter = MagicMock()
    adapter.db.__getitem__.side_effect = collection
    manager = MagicMock()
    manager.mongodb_adapter = adapter
    return manager, seen


@pytest.mark.asyncio
async def test_the_fill_loader_reads_fill_events_before_the_end():
    manager, seen = _db({"execution_events": [{"event_type": "filled"}]})
    api_module.db_manager = manager
    try:
        rows = await route._load_fills(T0)
        assert rows == [{"event_type": "filled"}]
        assert seen["execution_events"]["event_type"] == {
            "$in": ["filled", "partial_fill"]
        }
        assert seen["execution_events"]["timestamp"] == {"$lt": T0}
        await route._load_fills(None)
        assert "timestamp" not in seen["execution_events"]
    finally:
        api_module.db_manager = None


@pytest.mark.asyncio
async def test_the_stop_loader_reads_the_stop_a_position_carried():
    manager, seen = _db(
        {
            "positions": [
                {"position_id": "p1", "entry_price": 100, "stop_loss": 98},
                {
                    "position_id": "p2",
                    "avg_price": 50,
                    "stop_loss": 52,
                },  # no entry_price: avg_price
                {
                    "position_id": "p3",
                    "entry_price": 100,
                    "stop_loss": None,
                },  # no usable stop
                {"entry_price": 100, "stop_loss": 98},  # no id
            ]
        }
    )
    api_module.db_manager = manager
    try:
        stops = await route._load_stops(["p1", "p2", "p3"])
        assert await route._load_stops([]) == {}
    finally:
        api_module.db_manager = None
    assert stops == {"p1": D("0.02"), "p2": D("0.04")}
    assert seen["positions"] == {"position_id": {"$in": ["p1", "p2", "p3"]}}


@pytest.mark.asyncio
async def test_the_decision_loader_reads_the_stop_a_decision_carried():
    manager, seen = _db(
        {
            "cio_decisions": [
                {
                    "decision_id": "d1",
                    "source": "cio_llm",
                    "payload": {"entry_price": 100, "stop_loss": 99},
                },
                {"decision_id": "d2", "payload": {"stop_loss_pct": "0.03"}},
                {"decision_id": "d3", "payload": {"stop_loss_pct": "oops"}},
                {"decision_id": "d4", "price": 200, "payload": {"stop_loss": 190}},
                {"decision_id": "d5"},
            ]
        }
    )
    api_module.db_manager = manager
    try:
        decisions = await route._load_decisions(["d1", "d2", "d3", "d4", "d5"])
        assert await route._load_decisions([]) == {}
    finally:
        api_module.db_manager = None
    assert decisions["d1"] == {"source": "cio_llm", "stop_fraction": D("0.01")}
    assert decisions["d2"]["stop_fraction"] == D("0.03")
    assert decisions["d3"]["stop_fraction"] is None
    assert decisions["d4"]["stop_fraction"] == D("0.05")
    assert decisions["d5"] == {"source": None, "stop_fraction": None}
    assert seen["cio_decisions"] == {
        "decision_id": {"$in": ["d1", "d2", "d3", "d4", "d5"]}
    }


def test_an_http_error_from_a_loader_passes_through(monkeypatch):
    from fastapi import HTTPException

    async def teapot(end):
        raise HTTPException(status_code=418, detail="teapot")

    monkeypatch.setattr(route, "_load_fills", teapot)
    api_module.db_manager = MagicMock()
    try:
        response = TestClient(create_app()).get("/analysis/strategy-net-r")
    finally:
        api_module.db_manager = None
    assert response.status_code == 418


def test_a_decision_without_a_stop_does_not_hide_the_next_one():
    rows = _round_rows("A", 100, 110, 0, decision_id="d0")
    book = RoundBook()
    for row in rows:
        book.apply(row)
    closed = book.closed[0]
    closed.decision_ids = ("dmissing", "d1")
    scored = score_rounds(
        [closed], decisions={"dmissing": None, "d1": {"stop_fraction": D("0.04")}}
    )
    assert scored[0].stop_fraction == D("0.04")


def test_an_unreadable_number_in_a_fill_counts_as_zero():
    from data_manager.services.round_book import _dec

    assert (
        _dec("not a number") == D("0")
        and _dec(None) == D("0")
        and _dec("1.5") == D("1.5")
    )
