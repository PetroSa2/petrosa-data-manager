"""Every fill ends in a closed round, the open round or an explicit unattributed bucket (dm#537)."""

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance import round_report
from data_manager.services.round_book import RoundBook, build_report

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _fill(strategy, side, qty, price, minutes, symbol="ETHUSDT", **extra):
    return {
        "event_type": "filled",
        "strategy_id": strategy,
        "symbol": symbol,
        "side": side,
        "fill_qty": qty,
        "fill_price": price,
        "fill_time": T0 + timedelta(minutes=minutes),
        **extra,
    }


def _report(rows, **kwargs):
    return build_report(rows, now=NOW, **kwargs)


def _snapshot(at=NOW, rows=None):
    return {
        "as_of_ms": int(at.timestamp() * 1000),
        "rows": rows or [],
    }


def test_orphan_overlay_allocates_exchange_quantity_newest_first_across_strategies():
    rows = [
        _fill("old", "buy", 1.0, 100.0, 0, symbol="BTCUSDT", order_id="old-order"),
        _fill("new", "buy", 1.0, 101.0, 60, symbol="BTCUSDT", order_id="new-order"),
    ]
    report = _report(
        rows,
        exchange=_snapshot(
            rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]
        ),
        closed_entry_orders=set(),
    )

    old_leg = report["strategies"]["old"]["legs"]["LONG"]
    new_leg = report["strategies"]["new"]["legs"]["LONG"]
    assert old_leg["orphaned_quantity"] == pytest.approx(1.0)
    assert new_leg["held_quantity"] == pytest.approx(1.0)
    assert old_leg["open_lot_quantity"] == pytest.approx(
        old_leg["held_quantity"]
        + old_leg["orphaned_quantity"]
        + old_leg["ledger_closed_quantity"]
    )


def test_orphan_overlay_stale_snapshot_marks_nothing_and_preserves_statistics():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0, order_id="entry")]
    before = _report(rows)
    after = _report(
        rows,
        exchange=_snapshot(NOW - timedelta(seconds=1801), []),
        closed_entry_orders=set(),
    )
    assert after["orphan_overlay"] == "disabled_stale"
    assert after["strategies"]["s1"]["orphaned_quantity"] == 0
    assert (
        after["strategies"]["s1"]["closed_rounds"]
        == before["strategies"]["s1"]["closed_rounds"]
    )


def test_orphan_overlay_handles_flat_snapshot_and_ledger_closed_lots():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0, order_id="closed")]
    report = _report(
        rows,
        exchange=_snapshot(rows=[]),
        closed_entry_orders={"closed"},
    )
    leg = report["strategies"]["s1"]["legs"]["LONG"]
    assert leg["ledger_closed_quantity"] == pytest.approx(1.0)
    assert leg["orphaned_quantity"] == pytest.approx(0.0)


def test_orphan_overlay_maps_both_signs_and_holds_lots_newer_than_snapshot():
    snapshot_at = NOW - timedelta(minutes=5)
    rows = [
        _fill("long", "buy", 1.0, 100.0, 0, symbol="BTCUSDT", order_id="long"),
        _fill("short", "sell", 1.0, 100.0, 1, symbol="ETHUSDT", order_id="short"),
        _fill(
            "fresh",
            "buy",
            1.0,
            100.0,
            (NOW - T0).total_seconds() / 60,
            symbol="BTCUSDT",
            order_id="fresh",
        ),
    ]
    report = _report(
        rows,
        exchange=_snapshot(
            snapshot_at,
            [
                {"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"},
                {"symbol": "ETHUSDT", "position_side": "BOTH", "quantity": "-1"},
            ],
        ),
        closed_entry_orders=set(),
    )
    assert report["strategies"]["short"]["legs"]["SHORT"]["exchange_quantity"] == 1
    assert report["strategies"]["fresh"]["legs"]["LONG"]["held_quantity"] == 1


def test_orphan_overlay_does_not_mutate_book_or_closed_rounds():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, order_id="entry"),
        _fill("s1", "sell", 1.0, 101.0, 5, order_id="exit"),
    ]
    book = RoundBook()
    for row in rows:
        book.apply(row)
    before = repr(book._books)
    baseline = book.report(now=NOW)
    with_overlay = book.report(
        now=NOW,
        exchange=_snapshot(rows=[]),
        closed_entry_orders=set(),
    )
    assert repr(book._books) == before
    assert (
        with_overlay["strategies"]["s1"]["closed_rounds"]
        == baseline["strategies"]["s1"]["closed_rounds"]
    )
    assert (
        with_overlay["strategies"]["s1"]["wins"] == baseline["strategies"]["s1"]["wins"]
    )


def test_orphan_overlay_reports_partial_lot_excess_without_mutating_quantity():
    rows = [_fill("s1", "buy", 2.0, 100.0, 0, symbol="BTCUSDT", order_id="entry")]
    report = _report(
        rows,
        exchange=_snapshot(
            rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]
        ),
        closed_entry_orders=set(),
    )
    leg = report["strategies"]["s1"]["legs"]["LONG"]
    assert leg["held_quantity"] == pytest.approx(1.0)
    assert leg["orphaned_quantity"] == pytest.approx(1.0)
    assert leg["open_lot_quantity"] == pytest.approx(2.0)


def test_a_strategy_with_only_entries_shows_why_it_has_no_rounds():
    # bollinger_squeeze_alert: 225 fills, 0 closed rounds. Every fill is an entry; no exit is attributed to it.
    rows = [
        _fill("bollinger_squeeze_alert", "buy", 0.008, 2700 + i, i) for i in range(225)
    ]

    report = _report(rows)

    stats = report["strategies"]["bollinger_squeeze_alert"]
    assert stats["fills"] == 225
    assert stats["entry_fills"] == 225
    assert stats["exit_fills"] == 0
    assert stats["closed_rounds"] == 0
    assert stats["open_rounds"] == 1
    assert stats["fills_in_open_rounds"] == 225
    assert report["accounted"] is True


def test_exits_filed_under_another_strategy_id_are_not_lost_but_explained():
    rows = [
        _fill("bollinger_squeeze_alert", "buy", 1.0, 100.0, 0),
        _fill(
            "unknown", "sell", 1.0, 101.0, 30
        ),  # the exit, published without its strategy
        _fill("", "sell", 1.0, 99.0, 40),
    ]

    report = _report(rows)

    assert report["strategies"]["bollinger_squeeze_alert"]["closed_rounds"] == 0
    assert report["unattributed"] == {"placeholder_strategy_id": 1, "no_strategy_id": 1}
    assert report["totals"] == {
        "fills": 3,
        "attributed_to_a_strategy": 1,
        "unattributed": 2,
        "position_side_unknown": 1,
        "legacy_exit_side_mapped": 0,
    }


def test_unusable_fills_are_counted_with_their_reason():
    rows = [
        _fill("s1", "hold", 1.0, 100.0, 0),
        _fill("s1", "buy", None, 100.0, 1),
        _fill("s1", "buy", 1.0, 0, 2),
        {"event_type": "placed", "strategy_id": "s1"},  # not a fill at all: ignored
    ]

    report = _report(rows)

    assert report["unattributed"] == {"unusable_fill": 3}
    assert report["totals"]["fills"] == 3


def test_an_add_on_and_a_close_make_one_round_with_all_its_fills():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0),
        _fill("s1", "buy", 1.0, 102.0, 10),  # add-on
        _fill("s1", "sell", 2.0, 106.0, 90),
    ]

    stats = _report(rows)["strategies"]["s1"]

    assert stats["closed_rounds"] == 1
    assert stats["fills_in_closed_rounds"] == 3
    assert stats["open_rounds"] == 0
    assert stats["realized_pnl_closed_rounds"] == pytest.approx(
        (106 - 100) + (106 - 102)
    )
    assert stats["median_holding_seconds"] == 90 * 60
    assert stats["entry_fills"] == 2 and stats["exit_fills"] == 1


def test_a_partial_exit_leaves_the_round_open_until_the_position_is_flat():
    rows = [_fill("s1", "buy", 2.0, 100.0, 0), _fill("s1", "sell", 1.0, 101.0, 5)]
    assert _report(rows)["strategies"]["s1"]["closed_rounds"] == 0
    assert _report(rows)["strategies"]["s1"]["open_rounds"] == 1

    rows.append(_fill("s1", "sell", 1.0, 103.0, 20))
    stats = _report(rows)["strategies"]["s1"]

    assert stats["closed_rounds"] == 1
    assert stats["open_rounds"] == 0
    assert stats["median_holding_seconds"] == 20 * 60


def test_a_flip_closes_the_round_and_opens_the_next_one():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0), _fill("s1", "sell", 3.0, 101.0, 10)]

    report = _report(rows)
    stats = report["strategies"]["s1"]

    assert stats["closed_rounds"] == 1
    assert stats["open_rounds"] == 1  # the short opened by the remaining 2
    assert stats["fills"] == 2
    assert report["accounted"] is True


def test_symbols_and_strategies_have_their_own_rounds():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, symbol="ETHUSDT"),
        _fill("s1", "buy", 1.0, 50.0, 1, symbol="BTCUSDT"),
        _fill("s1", "sell", 1.0, 101.0, 2, symbol="ETHUSDT"),
        _fill("s2", "sell", 1.0, 99.0, 3, symbol="ETHUSDT"),
    ]

    report = _report(rows)

    assert report["strategies"]["s1"]["closed_rounds"] == 1
    assert report["strategies"]["s1"]["open_rounds"] == 1  # BTCUSDT
    assert report["strategies"]["s2"]["closed_rounds"] == 0


def test_the_rate_and_holding_time_cover_the_window_and_report_n():
    old = [
        _fill("s1", "buy", 1.0, 100.0, 0),
        _fill("s1", "sell", 1.0, 101.0, 60),  # closed on Oct 1, outside a 3-day window
    ]
    recent_start = (NOW - T0).total_seconds() / 60 - 60 * 24  # a day before now
    recent = [
        _fill("s1", "buy", 1.0, 100.0, recent_start),
        _fill("s1", "sell", 1.0, 99.0, recent_start + 120),
        _fill("s1", "buy", 1.0, 100.0, recent_start + 200),
        _fill("s1", "sell", 1.0, 102.0, recent_start + 500),
    ]

    stats = _report(old + recent, window_days=3)["strategies"]["s1"]

    assert stats["closed_rounds"] == 3
    assert stats["closed_rounds_in_window"] == 2
    assert stats["n"] == 2
    assert stats["closed_round_rate_per_day"] == pytest.approx(2 / 3)
    assert stats["median_holding_seconds"] == pytest.approx((120 * 60 + 300 * 60) / 2)


def test_no_closed_round_in_the_window_has_no_holding_time():
    stats = _report([_fill("s1", "buy", 1.0, 100.0, 0)])["strategies"]["s1"]

    assert stats["median_holding_seconds"] is None
    assert stats["n"] == 0
    assert stats["closed_round_rate_per_day"] == 0


def test_fills_in_any_input_order_give_the_same_rounds():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0),
        _fill("s1", "sell", 1.0, 101.0, 10),
    ]

    assert _report(list(reversed(rows)))["strategies"]["s1"]["closed_rounds"] == 1


def test_every_fill_is_always_accounted_for():
    rng = random.Random(737)
    strategies = ["s1", "s2", "unknown", ""]
    rows = [
        _fill(
            rng.choice(strategies),
            rng.choice(["buy", "sell"]),
            rng.choice([0.5, 1.0, 2.0]),
            100 + rng.random(),
            i,
            symbol=rng.choice(["ETHUSDT", "BTCUSDT"]),
        )
        for i in range(500)
    ]

    report = _report(rows)

    assert report["accounted"] is True
    total = sum(s["fills"] for s in report["strategies"].values()) + sum(
        report["unattributed"].values()
    )
    assert total == report["totals"]["fills"] == 500


def test_the_round_book_can_be_fed_one_fill_at_a_time():
    book = RoundBook()
    book.apply(_fill("s1", "buy", 1.0, 100.0, 0))
    book.apply(_fill("s1", "sell", 1.0, 101.0, 5))

    assert len(book.closed) == 1
    assert book.closed[0].holding_seconds == 300


@pytest.mark.asyncio
async def test_the_endpoint_reports_the_rounds_and_says_when_it_was_truncated():
    import data_manager.api.app as api_module
    from data_manager.api.routes.analysis import get_closed_rounds

    rows = [_fill("s1", "buy", 1.0, 100.0, 0), _fill("s1", "sell", 1.0, 101.0, 5)]
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    collection = MagicMock()
    collection.find.return_value = cursor
    api_module.db_manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"execution_events": collection})
    )
    try:
        report = await get_closed_rounds(strategy_id="s1", window_days=30.0)
    finally:
        api_module.db_manager = None

    assert report["strategies"]["s1"]["closed_rounds"] == 1
    assert report["metadata"]["fills_read"] == 2
    assert report["metadata"]["truncated"] is False
    assert collection.find.call_args.args[0]["strategy_id"] == "s1"


@pytest.mark.asyncio
async def test_the_endpoint_needs_the_database():
    from fastapi import HTTPException

    import data_manager.api.app as api_module
    from data_manager.api.routes.analysis import get_closed_rounds

    api_module.db_manager = None
    with pytest.raises(HTTPException) as excinfo:
        await get_closed_rounds(strategy_id=None, window_days=30.0)

    assert excinfo.value.status_code == 503


def test_the_cli_prints_a_line_per_strategy_and_fails_only_when_a_fill_is_unaccounted(
    monkeypatch, capsys
):
    report = build_report(
        [_fill("s1", "buy", 1.0, 100.0, 0), _fill("unknown", "sell", 1.0, 100.0, 1)],
        now=NOW,
    )

    async def fake_run(_args):
        return report

    monkeypatch.setattr(round_report, "run", fake_run)

    assert round_report.main(["--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "s1: fills=1 (entry=1 exit=0) closed_rounds=0 open_rounds=1" in out
    assert "unattributed fills: {'placeholder_strategy_id': 1}" in out

    report["accounted"] = False
    assert round_report.main([]) == 1


# --- hedge mode: rounds are kept per position side -------------------------------------------------


def test_hedge_legs_are_separate_rounds_not_netted():
    # BTCUSDT has a LONG and a SHORT open at once: a SELL on the LONG closes it, it does not open or
    # touch the SHORT.
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, position_side="LONG"),
        _fill("s1", "sell", 1.0, 90.0, 1, position_side="SHORT"),  # opens the SHORT
        _fill(
            "s1", "sell", 1.0, 110.0, 2, position_side="LONG"
        ),  # closes the LONG: +10
    ]
    report = _report(rows)
    stats = report["strategies"]["s1"]
    assert stats["closed_rounds"] == 1
    assert stats["open_rounds"] == 1  # the SHORT is still open
    assert stats["entry_fills"] == 2
    assert stats["exit_fills"] == 1
    assert stats["realized_pnl_closed_rounds"] == pytest.approx(10.0)
    assert stats["position_side_unknown"] == 0
    assert report["totals"]["position_side_unknown"] == 0
    assert report["accounted"] is True


def test_buy_on_a_short_is_an_exit_and_the_short_pnl_is_inverted():
    rows = [
        _fill("s1", "sell", 2.0, 100.0, 0, position_side="SHORT"),
        _fill("s1", "buy", 1.0, 95.0, 10, position_side="SHORT"),  # +5
        _fill("s1", "buy", 1.0, 105.0, 20, position_side="SHORT"),  # -5, flat
    ]
    stats = _report(rows)["strategies"]["s1"]
    assert stats["closed_rounds"] == 1
    assert stats["entry_fills"] == 1
    assert stats["exit_fills"] == 2
    assert stats["realized_pnl_closed_rounds"] == pytest.approx(0.0)
    assert stats["median_holding_seconds"] == pytest.approx(20 * 60)


def test_position_side_is_read_from_ps_and_from_the_stored_payload():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, ps="LONG"),
        _fill("s1", "sell", 1.0, 101.0, 1, payload={"position_side": "LONG"}),
    ]
    stats = _report(rows)["strategies"]["s1"]
    assert stats["closed_rounds"] == 1
    assert stats["position_side_unknown"] == 0


def test_a_leg_exit_without_an_open_lot_is_unattributed_not_a_flip():
    rows = [_fill("s1", "sell", 1.0, 100.0, 0, position_side="LONG")]
    report = _report(rows)
    assert report["unattributed"] == {"exit_without_entry": 1}
    assert "s1" not in report["strategies"]


def test_fills_without_a_position_side_are_netted_and_counted():
    # today's netting: BUY then SELL close a round, and both rows are counted as netted
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0),
        _fill("s1", "sell", 1.0, 101.0, 1),
        _fill(
            "s1", "buy", 1.0, 100.0, 2, position_side="BOTH"
        ),  # one-way: nets, not "unknown"
    ]
    report = _report(rows)
    stats = report["strategies"]["s1"]
    assert stats["closed_rounds"] == 1
    assert stats["position_side_unknown"] == 2
    assert report["totals"]["position_side_unknown"] == 2
    assert report["accounted"] is True


def test_hedge_and_netted_fills_stay_fully_accounted_under_random_input():
    rng = random.Random(7)
    rows = []
    for i in range(400):
        extra = rng.choice(
            [{}, {"position_side": "LONG"}, {"position_side": "SHORT"}, {"ps": "BOTH"}]
        )
        rows.append(
            _fill(
                rng.choice(["a", "b", "unknown"]),
                rng.choice(["buy", "sell"]),
                rng.choice([0.5, 1.0, 2.0]),
                100 + rng.random(),
                i,
                symbol=rng.choice(["ETHUSDT", "BTCUSDT"]),
                **extra,
            )
        )
    report = _report(rows)
    assert report["accounted"] is True
    assert report["totals"]["fills"] == 400


# --- the per-strategy fields the CIO's posterior and cold-start rules read (cio#297) -----------------


def test_report_carries_wins_losses_and_the_round_timestamps():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0),
        _fill("s1", "sell", 1.0, 110.0, 10),  # win
        _fill("s1", "buy", 1.0, 100.0, 20),
        _fill("s1", "sell", 1.0, 90.0, 30),  # loss
        _fill("s1", "buy", 1.0, 100.0, 40),
        _fill("s1", "sell", 1.0, 100.0, 50),  # flat: neither
        _fill("s1", "buy", 1.0, 100.0, 60),  # open round
    ]
    stats = _report(rows)["strategies"]["s1"]
    assert (stats["wins"], stats["losses"], stats["closed_rounds"]) == (1, 1, 3)
    assert stats["first_fill_at"] == (T0).isoformat()
    assert stats["last_closed_at"] == (T0 + timedelta(minutes=50)).isoformat()
    assert (
        stats["oldest_open_round_opened_at"] == (T0 + timedelta(minutes=60)).isoformat()
    )


def test_a_strategy_with_only_entries_has_no_closed_timestamp_and_an_old_open_round():
    rows = [_fill("s1", "buy", 1.0, 100.0, i) for i in range(5)]
    stats = _report(rows)["strategies"]["s1"]
    assert (stats["wins"], stats["losses"]) == (0, 0)
    assert stats["last_closed_at"] is None
    assert (
        stats["first_fill_at"] == stats["oldest_open_round_opened_at"] == T0.isoformat()
    )
