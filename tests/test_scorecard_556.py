"""Scorecard funding allocation and ledger conservation (petrosa-data-manager#556)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes import scorecard as route
from data_manager.db.repositories.ledger_repository import LedgerRepository
from data_manager.services.round_book import RoundBook
from data_manager.services.scorecard import (
    in_period,
    metrics,
    score_rounds,
    strategy_net_r,
)
from data_manager.services.scorecard_funding import (
    Exposure,
    allocate_funding,
    split,
)
from data_manager.services.scorecard_ledger import period_ledger

D = Decimal
DAY1 = date(2026, 10, 1)
DAY2 = date(2026, 10, 2)


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def fill(strategy, side, price, when, *, fee="0.5", pnl=None, qty=1, **extra):
    row = {
        "event_type": "filled",
        "strategy_id": strategy,
        "symbol": extra.pop("symbol", "BTCUSDT"),
        "side": side,
        "fill_qty": qty,
        "fill_price": price,
        "fill_time": when,
        **extra,
    }
    if fee is not None:
        row.update(fee=fee, fee_asset="USDT", fee_status="known")
    if pnl is not None:
        row["pnl"] = pnl
    return row


def book_of(rows):
    book = RoundBook()
    for row in sorted(rows, key=lambda r: r["fill_time"]):
        book.apply(row)
    return book


def exposure(key, opened, closed, notional="100", symbol="BTCUSDT", side="NET"):
    return Exposure(key, symbol, opened, closed, D(notional), side)


# --- the allocation ------------------------------------------------------------------------------


def test_a_split_is_exact():
    shares = split(D("-1"), [D("100"), D("100"), D("100")])
    assert sum(shares) == D("-1")
    assert split(D("-3.00000001"), [D("1"), D("2"), D("4")]) and sum(
        split(D("-3.00000001"), [D("1"), D("2"), D("4")])
    ) == D("-3.00000001")


def test_a_position_open_across_a_mark_pays_one_allocation_and_one_opened_after_pays_none():
    crossing = exposure(0, at(1, 7, 59), at(1, 8, 1))
    after = exposure(1, at(1, 8, 1), at(1, 9, 0))
    allocation = allocate_funding([crossing, after], {("BTCUSDT", DAY1): D("-3")})
    assert allocation.by_round_day == {(0, DAY1): D("-3")}
    assert allocation.unallocated == {}
    assert allocation.income_by_round() == {0: D("-3")}


def test_a_round_closed_exactly_at_a_mark_does_not_pay_it():
    allocation = allocate_funding(
        [exposure(0, at(1, 7, 0), at(1, 8, 0))], {("BTCUSDT", DAY1): D("-1")}
    )
    assert allocation.by_round_day == {}
    assert allocation.unallocated == {("BTCUSDT", DAY1): D("-1")}


def test_funding_is_shared_by_notional_time_and_allocated_plus_unallocated_is_the_total():
    long_held = exposure(0, at(1, 7, 0), at(1, 17, 0), "100")  # marks 08 and 16
    short_held = exposure(1, at(1, 7, 0), at(1, 9, 0), "100")  # mark 08
    other_symbol = exposure(2, at(1, 7, 0), at(1, 17, 0), "100", symbol="ETHUSDT")
    totals = {("BTCUSDT", DAY1): D("-3"), ("SOLUSDT", DAY1): D("-0.4")}
    allocation = allocate_funding([long_held, short_held, other_symbol], totals)
    assert allocation.by_round_day[(0, DAY1)] == D("-2")
    assert allocation.by_round_day[(1, DAY1)] == D("-1")
    assert (2, DAY1) not in allocation.by_round_day
    assert allocation.unallocated == {("SOLUSDT", DAY1): D("-0.4")}
    allocated = sum(allocation.by_round_day.values())
    assert allocated + sum(allocation.unallocated.values()) == D("-3.4")


def test_an_open_round_holds_marks_until_now_and_a_hedge_is_flagged():
    long_leg = Exposure(0, "BTCUSDT", at(1, 7, 0), None, D("100"), "LONG")
    short_leg = Exposure(1, "BTCUSDT", at(1, 7, 0), at(1, 9, 0), D("100"), "SHORT")
    allocation = allocate_funding([long_leg, short_leg], {("BTCUSDT", DAY1): D("-2")})
    assert ("BTCUSDT", DAY1) in allocation.hedged_symbol_days
    assert sum(allocation.by_round_day.values()) == D("-2")


# --- net includes funding ------------------------------------------------------------------------


def test_net_is_gross_minus_fees_minus_funding_and_flows_into_net_r_and_the_keep_kill_input():
    rows = [
        fill("A", "buy", 100, at(1, 7, 59), pnl=0),
        fill("A", "sell", 110, at(1, 8, 1), pnl=10),
    ]
    book = book_of(rows)
    scored = score_rounds(book.closed, stops={}, decisions={}, funding_cost={0: D("3")})
    assert scored[0].gross == D("10") and scored[0].fees == D("1")
    assert scored[0].net == D("6")  # 10 - 1 - 3
    m = metrics(scored, minimum=1)
    assert m["funding_allocated"] == "3" and m["net_pnl"] == "6"
    assert D(m["net_pnl"]) == D(m["gross_pnl"]) - D(m["fees"]) - D(
        m["funding_allocated"]
    )
    assert strategy_net_r(scored)["strategies"]["A"]["cumulative_net_loss_usd"] == "0"


def test_funding_can_turn_a_winner_into_a_net_loss_for_the_keep_kill_input():
    book = book_of(
        [
            fill("A", "buy", 100, at(1, 7, 59), pnl=0),
            fill("A", "sell", 100.5, at(1, 8, 1), pnl=0.5),
        ]
    )
    scored = score_rounds(book.closed, funding_cost={0: D("2")})
    assert scored[0].net == D("-2.5")
    assert strategy_net_r(scored)["strategies"]["A"]["cumulative_net_loss_usd"] == "2.5"


# --- conservation --------------------------------------------------------------------------------


def ledger_of(rows, start, end, funding=None):
    book = book_of(rows)
    exposures = [
        Exposure(
            i, r.symbol, r.opened_at, r.closed_at, r.entry_notional, r.position_side
        )
        for i, r in enumerate(book.closed)
    ] + [
        Exposure(len(book.closed) + j, o.symbol, o.opened_at, None, o.entry_notional)
        for j, o in enumerate(book.open_cycles())
    ]
    allocation = allocate_funding(exposures, funding) if funding is not None else None
    income = allocation.income_by_round() if allocation else {}
    scored = score_rounds(
        book.closed,
        funding_cost={i: -income.get(i, D(0)) for i in range(len(book.closed))},
    )
    selected = in_period(scored, start, end)
    ledger = period_ledger(
        closed=scored,
        selected=selected,
        open_rounds=book.open_cycles(),
        unattributed=book.unattributed_fills,
        start=start,
        end=end,
        funding_income=funding,
        allocation=allocation,
    )
    return ledger, scored, selected, book


def test_group_net_plus_unattributed_equals_the_ledger_net_when_all_rounds_close_in_the_period():
    rows = [
        fill("A", "buy", 100, at(1, 7, 59), pnl=0),
        fill("A", "sell", 110, at(1, 8, 1), pnl=10),
        fill("B", "buy", 50, at(1, 9, 0), pnl=0, symbol="ETHUSDT"),
        fill("B", "sell", 48, at(1, 10, 0), pnl=-2, symbol="ETHUSDT"),
        # a manual close: no strategy id
        fill("", "sell", 100, at(1, 11, 0), pnl=4, fee="0.25"),
    ]
    funding = {("BTCUSDT", DAY1): D("-3"), ("SOLUSDT", DAY1): D("-0.4")}
    ledger, scored, selected, _ = ledger_of(rows, at(1, 0), at(2, 0), funding)
    assert ledger["exact"] is True and ledger["residual"] == "0"
    assert ledger["difference"] == "0"
    assert ledger["total_net"] == ledger["ledger_net"]
    # A: 10 - 1 - 3 ; B: -2 - 1 ; unattributed: 4 - 0.25 - 0.4 (SOLUSDT funding no round held)
    assert D(ledger["groups_net"]) == D("6") + D("-3")
    assert ledger["unattributed_net"] == "3.35"
    assert ledger["unattributed_group"]["fills"] == 1
    assert ledger["unattributed_group"]["by_reason"] == {"no_strategy_id": 1}
    assert ledger["funding"]["conserved"] is True
    assert ledger["funding"]["exchange_total_cost"] == "3.4"
    assert ledger["funding"]["allocated_to_rounds"] == "3"
    assert ledger["funding"]["unallocated"] == "0.4"
    for name in (
        "open_entry_fee_delta",
        "exit_fee_boundary_delta",
        "realized_boundary_delta",
        "realized_basis_difference",
        "funding_boundary_delta",
    ):
        assert ledger[name] == "0"


def test_a_position_opened_on_day_d_and_closed_on_d_plus_1_has_net_gross_minus_both_fees_on_the_close_day():
    rows = [
        fill("A", "buy", 100, at(1, 22, 0), fee="0.5", pnl=0),
        fill("A", "sell", 110, at(2, 1, 0), fee="0.5", pnl=10),
    ]
    ledger, scored, selected, _ = ledger_of(rows, at(2, 0), at(3, 0))
    assert [r.net for r in selected] == [D("9")]  # 10 - 0.5 - 0.5
    assert (
        ledger["ledger_net"] == "9.5"
    )  # the close day books 10 - 0.5; the entry fee was booked on day D
    assert ledger["open_entry_fee_delta"] == "-0.5"
    assert ledger["exact"] is True
    # over both days the identity closes with no entry-fee delta
    both, *_ = ledger_of(rows, at(1, 0), at(3, 0))
    assert both["open_entry_fee_delta"] == "0" and both["difference"] == "0"


def test_entry_fees_of_rounds_not_closed_in_the_period_go_into_the_delta_with_the_opposite_sign():
    rows = [
        fill("A", "buy", 100, at(1, 22, 0), fee="0.5", pnl=0),
        fill("A", "sell", 110, at(2, 1, 0), fee="0.5", pnl=10),
        fill(
            "B", "buy", 50, at(2, 2, 0), fee="0.3", pnl=0, symbol="ETHUSDT"
        ),  # still open
    ]
    ledger, *_ = ledger_of(rows, at(2, 0), at(3, 0))
    # + 0.3 (entry fee B booked in the period) - 0.5 (entry fee A booked before)
    assert ledger["open_entry_fee_delta"] == "-0.2"
    assert ledger["exact"] is True


def test_partial_closes_across_the_boundary_are_named_not_hidden():
    rows = [
        fill("A", "buy", 100, at(1, 22, 0), qty=2, fee="1", pnl=0),
        fill("A", "sell", 110, at(1, 23, 0), qty=1, fee="0.5", pnl=10),
        fill("A", "sell", 120, at(2, 1, 0), qty=1, fee="0.5", pnl=20),
    ]
    ledger, _, selected, _ = ledger_of(rows, at(2, 0), at(3, 0))
    assert len(selected) == 1
    assert ledger["open_entry_fee_delta"] == "-1"
    assert ledger["exit_fee_boundary_delta"] == "-0.5"
    assert ledger["realized_boundary_delta"] == "10"
    assert ledger["exact"] is True


def test_a_reported_pnl_that_differs_from_fifo_is_a_named_basis_difference():
    rows = [
        fill("A", "buy", 100, at(1, 10, 0), pnl=0),
        fill(
            "A", "sell", 110, at(1, 11, 0), pnl=9.5
        ),  # the exchange reported 9.5, FIFO says 10
    ]
    ledger, *_ = ledger_of(rows, at(1, 0), at(2, 0))
    assert ledger["realized_basis_difference"] == "0.5"
    assert ledger["exact"] is True


def test_funding_of_days_outside_the_period_is_a_named_boundary_term():
    rows = [
        fill("A", "buy", 100, at(1, 7, 0), fee="0", pnl=0),
        fill("A", "sell", 100, at(2, 9, 0), fee="0", pnl=0),
    ]
    funding = {("BTCUSDT", DAY1): D("-1"), ("BTCUSDT", DAY2): D("-2")}
    ledger, scored, selected, _ = ledger_of(rows, at(2, 0), at(3, 0), funding)
    assert selected[0].funding == D("3")  # the round paid both days
    assert ledger["funding"]["exchange_total_cost"] == "2"  # the period's day only
    assert ledger["funding_boundary_delta"] == "-1"
    assert ledger["exact"] is True


def test_unavailable_funding_is_reported_and_left_out_not_guessed():
    rows = [
        fill("A", "buy", 100, at(1, 10, 0), pnl=0),
        fill("A", "sell", 110, at(1, 11, 0), pnl=10),
    ]
    ledger, *_ = ledger_of(rows, at(1, 0), at(2, 0), funding=None)
    assert ledger["funding"]["status"] == "unavailable"
    assert ledger["funding"]["exchange_total_cost"] is None
    assert ledger["exact"] is True


def test_fills_without_pnl_fall_back_to_fifo_and_are_counted():
    rows = [
        fill("A", "buy", 100, at(1, 10, 0)),
        fill("A", "sell", 110, at(1, 11, 0)),
    ]
    ledger, *_ = ledger_of(rows, at(1, 0), at(2, 0))
    assert ledger["fills_without_pnl_used_fifo"] == 2
    assert ledger["realized_basis_difference"] == "0" and ledger["exact"] is True


def test_the_round_book_keeps_a_fill_log_and_the_unattributed_amounts():
    book = book_of(
        [
            fill("A", "buy", 100, at(1, 10, 0), fee="0.5", pnl=0),
            fill("A", "sell", 110, at(1, 11, 0), fee="0.5", pnl=10),
            fill("", "sell", 100, at(1, 12, 0), fee="0.25", pnl=4),
        ]
    )
    log = book.closed[0].log
    assert [e.is_entry for e in log] == [True, False]
    assert [e.realized for e in log] == [D("0"), D("10")]
    assert [e.fee for e in log] == [D("0.5"), D("0.5")]
    (unattributed,) = book.unattributed_fills
    assert unattributed.reason == "no_strategy_id"
    assert unattributed.reported_pnl == D("4") and unattributed.fee == D("0.25")


# --- the ledger repository -----------------------------------------------------------------------


class _Adapter:
    def __init__(self, engine):
        self._engine = engine

    def _ensure_connected(self):
        return self._engine


@pytest.fixture
def ledger_repository():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ledger_exchange_day_revision (day DATE, revision INT)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE ledger_exchange_daily "
                "(day DATE, revision INT, symbol TEXT, funding_fee NUMERIC)"
            )
        )
        for day, revision, symbol, value in (
            ("2026-10-01", 1, "BTCUSDT", "-1"),
            (
                "2026-10-01",
                2,
                "BTCUSDT",
                "-1.5",
            ),  # a restated day: the latest revision counts
            ("2026-10-01", 2, "ETHUSDT", "0.25"),
            (
                "2026-10-01",
                2,
                "",
                "5",
            ),  # income with no symbol is not funding of a round
            ("2026-10-03", 1, "BTCUSDT", "-2"),
        ):
            conn.execute(
                sa.text("INSERT INTO ledger_exchange_daily VALUES (:d,:r,:s,:v)"),
                {"d": day, "r": revision, "s": symbol, "v": value},
            )
        for day, revision in (("2026-10-01", 1), ("2026-10-01", 2), ("2026-10-03", 1)):
            conn.execute(
                sa.text("INSERT INTO ledger_exchange_day_revision VALUES (:d,:r)"),
                {"d": day, "r": revision},
            )
    return LedgerRepository(_Adapter(engine), None)


def test_funding_by_symbol_day_uses_the_latest_revision_and_reports_the_days_present(
    ledger_repository,
):
    funding, present = ledger_repository.funding_by_symbol_day(DAY1, date(2026, 10, 3))
    assert funding == {
        ("BTCUSDT", DAY1): D("-1.5"),
        ("ETHUSDT", DAY1): D("0.25"),
        ("BTCUSDT", date(2026, 10, 3)): D("-2"),
    }
    assert present == {
        DAY1,
        date(2026, 10, 3),
    }  # 10-02 has no revision: missing, not zero


# --- the route -----------------------------------------------------------------------------------

ROUTE_ROWS = [
    fill("A", "buy", 100, at(1, 7, 59), pnl=0),
    fill("A", "sell", 110, at(1, 8, 1), pnl=10),
    fill("", "sell", 100, at(1, 11, 0), pnl=4, fee="0.25"),
]


@pytest.fixture
def client(monkeypatch):
    async def fills(end):
        return list(ROUTE_ROWS)

    async def none(ids):
        return {}

    monkeypatch.setattr(route, "_load_fills", fills)
    monkeypatch.setattr(route, "_load_stops", none)
    monkeypatch.setattr(route, "_load_decisions", none)
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


def test_the_scorecard_route_allocates_funding_and_reconciles_with_the_ledger(
    client, monkeypatch
):
    async def funding(first, last):
        assert first == DAY1
        return {("BTCUSDT", DAY1): D("-3"), ("SOLUSDT", DAY1): D("-0.4")}, {DAY1}

    monkeypatch.setattr(route, "_load_funding", funding)
    body = client.get(
        "/analysis/scorecard",
        params={"from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"},
    ).json()
    group = body["groups"]["A"]
    assert group["funding_allocated"] == "3"
    assert group["net_pnl"] == "6"
    assert body["unattributed"]["fills"] == 1
    assert body["unattributed"]["funding_allocated"] == "0.4"
    assert body["total_funding"] == "3.4"
    assert body["total_net"] == body["conservation"]["ledger_net"]
    assert body["conservation"]["exact"] is True
    assert body["funding"]["status"] == "allocated"
    assert body["funding"]["marks_utc"] == [0, 8, 16]
    assert body["funding"]["conserved"] is True
    assert body["open_entry_fee_delta"] == "0"
    assert body["groups_net"] == "6"


def test_the_route_serves_without_funding_when_the_ledger_is_unavailable(
    client, monkeypatch
):
    async def broken(first, last):
        raise RuntimeError("MySQL is unavailable")

    monkeypatch.setattr(route, "_load_funding", broken)
    response = client.get("/analysis/scorecard")
    assert response.status_code == 200
    body = response.json()
    assert body["funding"]["status"] == "unavailable"
    assert "MySQL is unavailable" in body["funding"]["error"]
    assert body["total_funding"] is None
    assert body["groups"]["A"]["funding_allocated"] == "0"
    assert body["groups"]["A"]["net_pnl"] == "9"  # no funding guessed


def test_the_keep_kill_input_includes_funding(client, monkeypatch):
    async def funding(first, last):
        return {("BTCUSDT", DAY1): D("-12")}, {DAY1}

    monkeypatch.setattr(route, "_load_funding", funding)
    body = client.get("/analysis/strategy-net-r").json()
    assert body["strategies"]["A"]["cumulative_net_loss_usd"] == "3"  # 10 - 1 - 12
