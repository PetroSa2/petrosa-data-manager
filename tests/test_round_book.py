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

    old_leg = report["strategies"]["old"]["legs"]["BTCUSDT"]["LONG"]
    new_leg = report["strategies"]["new"]["legs"]["BTCUSDT"]["LONG"]
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
    leg = report["strategies"]["s1"]["legs"]["ETHUSDT"]["LONG"]
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
    assert (
        report["strategies"]["short"]["legs"]["ETHUSDT"]["SHORT"]["exchange_quantity"]
        == 1
    )
    assert (
        report["strategies"]["fresh"]["legs"]["BTCUSDT"]["LONG"]["held_quantity"] == 1
    )


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


def test_report_mode_preserves_open_round_fields_until_apply():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0, order_id="entry")]
    baseline = _report(rows)
    report_mode = _report(
        rows,
        exchange=_snapshot(rows=[]),
        closed_entry_orders=set(),
    )
    apply_mode = _report(
        rows,
        exchange=_snapshot(rows=[]),
        closed_entry_orders=set(),
        apply_overlay=True,
    )
    base_stats = baseline["strategies"]["s1"]
    report_stats = report_mode["strategies"]["s1"]
    assert {key: report_stats[key] for key in base_stats} == base_stats
    assert apply_mode["strategies"]["s1"]["open_rounds"] == 0


def test_apply_mode_preserves_open_rounds_when_snapshot_is_not_usable():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0, order_id="entry")]
    baseline = _report(rows)["strategies"]["s1"]
    for snapshot in (
        _snapshot(NOW - timedelta(seconds=1801)),
        {"as_of_ms": None, "rows": []},
    ):
        applied = _report(
            rows,
            exchange=snapshot,
            closed_entry_orders=set(),
            apply_overlay=True,
        )["strategies"]["s1"]
        assert applied["open_rounds"] == baseline["open_rounds"]
        assert (
            applied["oldest_open_round_opened_at"]
            == baseline["oldest_open_round_opened_at"]
        )


def test_apply_mode_keeps_a_partly_held_lot_in_the_open_round():
    rows = [_fill("s1", "buy", 2.0, 100.0, 0, symbol="BTCUSDT", order_id="entry")]
    report = _report(
        rows,
        exchange=_snapshot(
            rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]
        ),
        closed_entry_orders=set(),
        apply_overlay=True,
    )
    stats = report["strategies"]["s1"]
    leg = stats["legs"]["BTCUSDT"]["LONG"]
    assert stats["open_rounds"] == 1
    assert stats["oldest_open_round_opened_at"] == (T0).isoformat()
    assert leg["held_quantity"] == pytest.approx(1.0)
    assert leg["orphaned_quantity"] == pytest.approx(1.0)


def test_orphan_overlay_uses_relative_quantity_tolerance():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT")]
    report = _report(
        rows,
        exchange=_snapshot(
            rows=[
                {
                    "symbol": "BTCUSDT",
                    "position_side": "LONG",
                    "quantity": "0.99999999995",
                }
            ]
        ),
        closed_entry_orders=set(),
    )
    assert report["strategies"]["s1"]["orphaned_quantity"] == 0


def test_late_exit_closes_a_book_even_when_the_overlay_would_have_orphaned_it():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT"),
        _fill("s1", "sell", 1.0, 101.0, 1, symbol="BTCUSDT"),
    ]
    report = _report(
        rows,
        exchange=_snapshot(
            rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "0"}]
        ),
        closed_entry_orders=set(),
        apply_overlay=True,
    )
    assert report["strategies"]["s1"]["closed_rounds"] == 1
    assert report["strategies"]["s1"]["orphaned_lots"] == 0


def test_overlay_keeps_same_leg_symbols_separate_and_matches_missing_order_ids_by_identity():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT"),
        _fill("s1", "buy", 1.0, 100.0, 1, symbol="ETHUSDT"),
    ]
    report = _report(
        rows,
        exchange=_snapshot(rows=[]),
        closed_entry_orders=set(),
    )
    assert (
        report["strategies"]["s1"]["legs"]["BTCUSDT"]["LONG"]["orphaned_quantity"] == 1
    )
    assert (
        report["strategies"]["s1"]["legs"]["ETHUSDT"]["LONG"]["orphaned_quantity"] == 1
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
    leg = report["strategies"]["s1"]["legs"]["BTCUSDT"]["LONG"]
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
    assert "strategy_id" not in collection.find.call_args.args[0]
    assert set(report["strategies"]) == {"s1"}


@pytest.mark.asyncio
async def test_rounds_endpoint_apply_excludes_ledger_closed_lots(monkeypatch):
    import data_manager.api.app as api_module
    import data_manager.db.repositories.ledger_repository as ledger_module
    from data_manager.api.routes.analysis import get_closed_rounds

    class FakeRepository:
        def __init__(self, *_args):
            pass

        def round_overlay_snapshot(self):
            return _snapshot(datetime.now(UTC), [])

        def closed_entry_order_ids(self):
            return {"closed"}

    rows = [_fill("s1", "buy", 1.0, 100.0, 0, order_id="closed")]
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    collection = MagicMock()
    collection.find.return_value = cursor
    api_module.db_manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"execution_events": collection}),
        mysql_adapter=object(),
    )
    monkeypatch.setattr(ledger_module, "LedgerRepository", FakeRepository)
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    try:
        report = await get_closed_rounds(strategy_id="s1", window_days=30.0)
    finally:
        api_module.db_manager = None

    stats = report["strategies"]["s1"]
    leg = stats["legs"]["ETHUSDT"]["LONG"]
    assert stats["open_rounds"] == 0
    assert stats["ledger_closed_rounds"] == 1
    assert leg["ledger_closed_quantity"] == pytest.approx(1.0)


def _routed_collection(by_strategy_filter):
    """An execution_events collection whose find() answers per query (all fills, or one strategy)."""
    calls = []

    def find(query, *args):
        calls.append(query)
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.to_list = AsyncMock(
            side_effect=lambda length=None: by_strategy_filter(query, length)
        )
        return cursor

    collection = MagicMock()
    collection.find.side_effect = find
    return collection, calls


@pytest.mark.asyncio
async def test_filtered_call_takes_its_figures_from_its_own_fills_when_all_fills_are_cut(
    monkeypatch,
):
    """The read of all fills is cut at the cap: the strategy's figures come from a read of ITS fills."""
    import data_manager.api.app as api_module
    import data_manager.api.routes.analysis as analysis_route
    from data_manager.api.routes.analysis import get_closed_rounds

    own = [_fill("s1", "buy", 1.0, 100.0, i * 10) for i in range(3)] + [
        _fill("s1", "sell", 3.0, 101.0, 100)
    ]
    # the cut read holds only the first fills of the account, none of them a close of s1
    cut = [_fill("s2", "buy", 1.0, 100.0, 0)]

    def answer(query, length):
        return own if query.get("strategy_id") == "s1" else cut

    collection, calls = _routed_collection(answer)
    api_module.db_manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"execution_events": collection})
    )
    monkeypatch.setattr(analysis_route, "_ROUND_MAX_FILLS", 1)
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "report")
    try:
        report = await get_closed_rounds(strategy_id="s1", window_days=30.0)
    finally:
        api_module.db_manager = None

    assert report["orphan_overlay"] == "disabled_truncated"
    assert set(report["strategies"]) == {"s1"}
    assert report["strategies"]["s1"]["closed_rounds"] == 1  # from its own fills
    assert report["strategies"]["s1"]["fills"] == 4
    assert report["metadata"]["fills_read"] == 4
    assert [("strategy_id" in call) for call in calls] == [False, True]


@pytest.mark.asyncio
async def test_filtered_call_slices_the_complete_replay_and_keeps_the_totals_of_its_strategy(
    monkeypatch,
):
    import data_manager.api.app as api_module
    from data_manager.api.routes.analysis import get_closed_rounds

    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0),
        _fill("s1", "sell", 1.0, 101.0, 5),
        _fill("s2", "buy", 1.0, 100.0, 0),
        {"event_type": "filled", "strategy_id": "unknown", "symbol": "ETHUSDT"},
    ]
    collection, calls = _routed_collection(lambda query, length: rows)
    api_module.db_manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"execution_events": collection})
    )
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "report")
    try:
        unfiltered = await get_closed_rounds(strategy_id=None, window_days=30.0)
        filtered = await get_closed_rounds(strategy_id="s1", window_days=30.0)
    finally:
        api_module.db_manager = None

    assert set(filtered["strategies"]) == {"s1"}
    assert filtered["strategies"]["s1"] == unfiltered["strategies"]["s1"]
    assert filtered["totals"]["fills"] == 2
    assert filtered["unattributed"] == {}
    assert filtered["accounted"] is True
    assert unfiltered["totals"]["unattributed"] == 1


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


def test_netted_flip_tags_the_flipping_fill_on_the_new_round():
    book = RoundBook()
    book.apply(_fill("s1", "buy", 1.0, 100.0, 0, decision_id="E"))
    book.apply(_fill("s1", "sell", 2.0, 101.0, 1, decision_id="X"))

    assert book.closed[0].decision_ids == ("E", "X")
    cycle = book._books[("s1", "ETHUSDT", "NET")].cycle
    assert cycle is not None
    assert cycle.decision_ids == ["X"]

    book.apply(_fill("s1", "buy", 1.0, 100.0, 2, decision_id="Z"))
    assert cycle.decision_ids == ["X", "Z"]


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


def test_ledger_repository_reads_overlay_inputs_without_writes():
    from data_manager.db.repositories.ledger_repository import LedgerRepository

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

        def all(self):
            return self.rows

        def scalars(self):
            return self

    repo = LedgerRepository(object(), None)

    def run(statement, _params=None):
        if "snapshot" in statement:
            return Result([{"as_of_ms": 1_000}])
        if "ledger_exchange_positions" in statement:
            return Result(
                [{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]
            )
        return Result(["entry-order"])

    repo._run = run
    assert repo.round_overlay_snapshot() == {
        "as_of_ms": 1_000,
        "rows": [{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}],
    }
    assert repo.closed_entry_order_ids() == {"entry-order"}


@pytest.mark.asyncio
async def test_endpoint_loads_overlay_inputs_and_reports_fresh_mode(monkeypatch):
    import data_manager.api.app as api_module
    import data_manager.db.repositories.ledger_repository as ledger_module
    from data_manager.api.routes.analysis import get_closed_rounds

    rows = [_fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT", order_id="entry")]
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    collection = MagicMock()
    collection.find.return_value = cursor

    class FakeRepository:
        def __init__(self, *_args):
            pass

        def round_overlay_snapshot(self):
            return {"as_of_ms": int(datetime.now(UTC).timestamp() * 1000), "rows": []}

        def closed_entry_order_ids(self):
            return set()

    monkeypatch.setattr(ledger_module, "LedgerRepository", FakeRepository)
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "report")
    api_module.db_manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"execution_events": collection}),
        mysql_adapter=object(),
    )
    try:
        report = await get_closed_rounds(strategy_id=None, window_days=30.0)
    finally:
        api_module.db_manager = None
    assert report["orphan_overlay"] == "enabled"
    assert report["exchange_snapshot"] == "fresh"
    assert report["strategies"]["s1"]["orphaned_lots"] == 1


# ----------------------------------------------------------------------------------------------
# Provability and production shapes (petrosa-data-manager#576, spec 3.8)
# ----------------------------------------------------------------------------------------------
OPEN_ROUND_KEYS = {
    "open_rounds",
    "oldest_open_round_opened_at",
    "first_fill_at",
    "fills_in_open_rounds",
}


def _iceberg_fills():
    """26 BUY entries and 3 SELLs on BTCUSDT, none carrying a position side (a NET book)."""
    rows = [
        _fill(
            "iceberg_detector",
            "buy",
            0.001,
            100000.0 + i,
            i * 100,
            symbol="BTCUSDT",
            order_id=f"ice-buy-{i}",
        )
        for i in range(26)
    ]
    rows += [
        _fill(
            "iceberg_detector",
            "sell",
            0.001,
            100500.0 + i,
            3000 + i * 10,
            symbol="BTCUSDT",
            order_id=f"ice-sell-{i}",
        )
        for i in range(3)
    ]
    return rows


def _production_fills():
    rows = _iceberg_fills()
    # ichimoku: seven closed hedge-mode rounds, then an open one that the exchange still holds
    for n in range(7):
        base = 5000 + n * 60
        rows.append(
            _fill(
                "ichimoku",
                "buy",
                1.0,
                2000.0,
                base,
                symbol="ETHUSDT",
                position_side="LONG",
                order_id=f"ich-in-{n}",
                fee=0.1,
                fee_asset="USDT",
                payload={"position_id": f"ich-{n}", "decision_id": f"dec-{n}"},
            )
        )
        rows.append(
            _fill(
                "ichimoku",
                "sell",
                1.0,
                2010.0 - 30 * (n % 2),
                base + 30,
                symbol="ETHUSDT",
                position_side="LONG",
                order_id=f"ich-out-{n}",
                fee=0.1,
                fee_asset="USDT",
                payload={"position_id": f"ich-{n}", "decision_id": f"dec-{n}"},
            )
        )
    rows.append(
        _fill(
            "ichimoku",
            "buy",
            2.0,
            2000.0,
            6000,
            symbol="ETHUSDT",
            position_side="LONG",
            order_id="ich-open",
        )
    )
    # rsi: entries only, on a leg the exchange no longer holds
    rows += [
        _fill(
            "rsi",
            "sell",
            0.5,
            50.0,
            200 + i * 30,
            symbol="SOLUSDT",
            position_side="SHORT",
            order_id=f"rsi-{i}",
        )
        for i in range(4)
    ]
    # volume_surge: a closed round plus entries (netted) on a second symbol
    rows += [
        _fill("volume", "buy", 1.0, 10.0, 300, symbol="XRPUSDT", order_id="vol-1"),
        _fill("volume", "sell", 1.0, 11.0, 320, symbol="XRPUSDT", order_id="vol-2"),
        _fill("volume", "buy", 3.0, 10.0, 400, symbol="XRPUSDT", order_id="vol-3"),
    ]
    return rows


def _production_exchange():
    return _snapshot(
        rows=[
            {"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "0.0006"},
            {"symbol": "ETHUSDT", "position_side": "LONG", "quantity": "2"},
        ]
    )


def _net_r(book):
    from data_manager.services.strategy_net_r import score_rounds, strategy_net_r

    return strategy_net_r(score_rounds(book.closed))


def test_replay_with_and_without_the_overlay_gives_identical_figures_for_every_strategy():
    rows = _production_fills()
    plain = _report(rows)
    report_mode = _report(
        rows, exchange=_production_exchange(), closed_entry_orders={"ich-open"}
    )
    applied = _report(
        rows,
        exchange=_production_exchange(),
        closed_entry_orders={"ich-open"},
        apply_overlay=True,
    )

    assert set(plain["strategies"]) == {"iceberg_detector", "ichimoku", "rsi", "volume"}
    for strategy, base in plain["strategies"].items():
        # report mode: every field of the plain report is identical
        assert {k: report_mode["strategies"][strategy][k] for k in base} == base
        # apply mode: only the open-round fields may move
        for key in set(base) - OPEN_ROUND_KEYS:
            assert applied["strategies"][strategy][key] == base[key], (strategy, key)
    assert report_mode["totals"] == plain["totals"] == applied["totals"]
    assert report_mode["unattributed"] == plain["unattributed"]
    assert report_mode["accounted"] is True
    assert applied["accounted"] is True

    def book_of(**kwargs):
        book = RoundBook()
        for row in sorted(rows, key=lambda r: r["fill_time"]):
            book.apply(row)
        before = (repr(book._books), repr(book.closed), _net_r(book))
        book.report(now=NOW, **kwargs)
        return before, (repr(book._books), repr(book.closed), _net_r(book))

    before, after = book_of(
        exchange=_production_exchange(),
        closed_entry_orders={"ich-open"},
        apply_overlay=True,
    )
    assert before == after  # books, closed rounds and strategy_net_r untouched
    assert before[2]["strategies"]["ichimoku"]["closed_rounds"] == 7


def test_iceberg_oldest_lots_are_orphaned_when_the_exchange_holds_0_0006():
    rows = _iceberg_fills()
    exchange = _snapshot(
        rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "0.0006"}]
    )
    plain = _report(rows)["strategies"]["iceberg_detector"]
    report_mode = _report(rows, exchange=exchange, closed_entry_orders=set())
    applied = _report(
        rows, exchange=exchange, closed_entry_orders=set(), apply_overlay=True
    )

    leg = report_mode["strategies"]["iceberg_detector"]["legs"]["BTCUSDT"]["LONG"]
    # 26 BUY - 3 SELL (FIFO, oldest lots consumed) = 23 lots of 0.001 open
    assert leg["open_lot_quantity"] == pytest.approx(0.023)
    assert leg["exchange_quantity"] == pytest.approx(0.0006)
    assert leg["held_quantity"] == pytest.approx(0.0006)
    assert leg["orphaned_quantity"] == pytest.approx(0.0224)
    # the 22 oldest lots are fully orphaned; the newest one is only partly (0.0004 of 0.001)
    assert len(leg["orphaned"]) == 23
    assert leg["orphaned"][0]["quantity"] == pytest.approx(0.0004)  # newest first
    assert leg["orphaned"][-1]["order_id"] == "ice-buy-3"  # oldest open lot
    assert leg["held_quantity"] + leg["orphaned_quantity"] == pytest.approx(0.023)
    # report mode keeps the stale 256 h figures; apply moves to the held lot
    assert (
        report_mode["strategies"]["iceberg_detector"]["oldest_open_round_opened_at"]
        == plain["oldest_open_round_opened_at"]
    )
    stats = applied["strategies"]["iceberg_detector"]
    assert stats["open_rounds"] == 1
    assert (
        stats["oldest_open_round_opened_at"]
        == (T0 + timedelta(minutes=25 * 100)).isoformat()
    )
    assert stats["oldest_open_round_opened_at"] != plain["oldest_open_round_opened_at"]
    assert stats["closed_rounds"] == plain["closed_rounds"]


def test_a_net_leg_of_short_lots_is_allocated_on_the_short_side():
    rows = [
        _fill("s1", "sell", 1.0, 100.0, 0, symbol="BTCUSDT", order_id="a"),
        _fill("s1", "sell", 1.0, 100.0, 60, symbol="BTCUSDT", order_id="b"),
        _fill("s1", "buy", 0.5, 99.0, 90, symbol="BTCUSDT", order_id="c"),
    ]
    report = _report(
        rows,
        exchange=_snapshot(
            rows=[{"symbol": "BTCUSDT", "position_side": "SHORT", "quantity": "-0.6"}]
        ),
        closed_entry_orders=set(),
        apply_overlay=True,
    )

    stats = report["strategies"]["s1"]
    assert set(stats["legs"]["BTCUSDT"]) == {"SHORT"}  # the side of the current lots
    leg = stats["legs"]["BTCUSDT"]["SHORT"]
    assert leg["open_lot_quantity"] == pytest.approx(1.5)  # 2.0 sold - 0.5 bought back
    assert leg["exchange_quantity"] == pytest.approx(0.6)
    assert leg["held_quantity"] == pytest.approx(0.6)
    assert leg["orphaned_quantity"] == pytest.approx(0.9)
    assert stats["open_rounds"] == 1
    assert (
        stats["oldest_open_round_opened_at"] == (T0 + timedelta(minutes=60)).isoformat()
    )


def test_report_and_apply_differ_only_in_the_open_round_fields_on_multi_lot_books():
    rows = (
        [
            _fill(
                "a", "buy", 1.0, 100.0, i * 30, symbol="BTCUSDT", position_side="LONG"
            )
            for i in range(4)
        ]
        + [
            _fill(
                "b",
                "buy",
                2.0,
                100.0,
                15 + i * 30,
                symbol="BTCUSDT",
                position_side="LONG",
            )
            for i in range(3)
        ]
        + [_fill("a", "buy", 1.0, 5.0, 10, symbol="ETHUSDT", position_side="LONG")]
    )
    exchange = _snapshot(
        rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "3"}]
    )
    plain = _report(rows)
    report_mode = _report(rows, exchange=exchange, closed_entry_orders=set())
    applied = _report(
        rows, exchange=exchange, closed_entry_orders=set(), apply_overlay=True
    )

    for strategy in ("a", "b"):
        base = plain["strategies"][strategy]
        in_report = report_mode["strategies"][strategy]
        in_apply = applied["strategies"][strategy]
        assert {k: in_report[k] for k in base} == base
        assert (
            in_report["legs"] == in_apply["legs"]
        )  # the same allocation in both modes
        assert in_report["orphaned_quantity"] == in_apply["orphaned_quantity"]
    # BTCUSDT: lots of a at 0,30,60,90 and of b at 15,45,75 (2.0 each); the exchange holds 3.0 of the newest
    b_leg = applied["strategies"]["b"]["legs"]["BTCUSDT"]["LONG"]
    a_leg = applied["strategies"]["a"]["legs"]["BTCUSDT"]["LONG"]
    assert b_leg["held_quantity"] + a_leg["held_quantity"] == pytest.approx(3.0)
    assert b_leg["held_quantity"] == pytest.approx(2.0)  # b@75 (2.0) is the newest lot
    assert a_leg["held_quantity"] == pytest.approx(1.0)  # a@90 (1.0)
    # a: the BTCUSDT book keeps its held lot (the newest, minute 90); its ETHUSDT lot is orphaned (no row)
    assert applied["strategies"]["a"]["open_rounds"] == 1
    assert (
        applied["strategies"]["a"]["oldest_open_round_opened_at"]
        == (T0 + timedelta(minutes=90)).isoformat()
    )
    assert applied["strategies"]["a"]["fills_in_orphaned_rounds"] == 1
    assert applied["accounted"] is True


def test_a_real_late_exit_after_the_snapshot_closes_the_orphaned_lot():
    entry = _fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT", order_id="entry")
    snapshot_at = T0 + timedelta(hours=4)
    now = snapshot_at + timedelta(minutes=10)
    exchange = _snapshot(
        at=snapshot_at,
        rows=[],  # flat at the snapshot: the lot is orphaned
    )
    late_exit = _fill(
        "s1", "sell", 1.0, 101.0, 4 * 60 + 5, symbol="BTCUSDT", order_id="late-exit"
    )

    before = build_report(
        [entry],
        now=now,
        exchange=exchange,
        closed_entry_orders=set(),
        apply_overlay=True,
    )["strategies"]["s1"]
    after = build_report(
        [entry, late_exit],
        now=now,
        exchange=exchange,
        closed_entry_orders=set(),
        apply_overlay=True,
    )

    assert before["orphaned_lots"] == 1
    assert before["open_rounds"] == 0
    stats = after["strategies"]["s1"]
    assert stats["closed_rounds"] == 1
    assert stats["orphaned_lots"] == 0
    assert stats["open_rounds"] == 0
    assert after["unattributed"] == {}  # no exit_without_entry noise
    assert after["accounted"] is True
    assert "SHORT" not in stats["legs"].get(
        "BTCUSDT", {}
    )  # no ghost short on a NET book


def test_apply_mode_counts_the_fills_of_a_fully_orphaned_book_so_the_check_balances():
    rows = [
        _fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT"),
        _fill("s1", "buy", 1.0, 100.0, 10, symbol="BTCUSDT"),
    ]
    applied = _report(
        rows, exchange=_snapshot(rows=[]), closed_entry_orders=set(), apply_overlay=True
    )

    stats = applied["strategies"]["s1"]
    assert stats["open_rounds"] == 0
    assert stats["fills_in_open_rounds"] == 0
    assert stats["fills_in_orphaned_rounds"] == 2
    assert applied["accounted"] is True
    # report mode and the plain report are unchanged: no new field, the fills stay in the open round
    plain = _report(rows)["strategies"]["s1"]
    report_mode = _report(rows, exchange=_snapshot(rows=[]), closed_entry_orders=set())[
        "strategies"
    ]["s1"]
    assert "fills_in_orphaned_rounds" not in report_mode
    assert report_mode["fills_in_open_rounds"] == plain["fills_in_open_rounds"] == 2


def test_a_lot_orphaned_to_within_the_tolerance_is_not_an_open_round():
    rows = [_fill("s1", "buy", 1.0, 100.0, 0, symbol="BTCUSDT")]
    applied = _report(
        rows,
        exchange=_snapshot(
            rows=[{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1e-12"}]
        ),
        closed_entry_orders=set(),
        apply_overlay=True,
    )

    stats = applied["strategies"]["s1"]
    assert stats["open_rounds"] == 0
    assert stats["oldest_open_round_opened_at"] is None
    assert stats["fills_in_orphaned_rounds"] == 1
    assert applied["accounted"] is True
