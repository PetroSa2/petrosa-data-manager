"""Legacy exit fills whose ``side`` is the position side are read as the closing order side (dm#550)."""

from datetime import UTC, datetime, timedelta

import pytest

from data_manager.services.fill_side import is_exit_shaped, legacy_exit_side, order_side
from data_manager.services.pnl_calculator import PnlCalculator
from data_manager.services.round_book import build_report

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _fill(side, qty, price, minutes, strategy="bollinger_squeeze_alert", **extra):
    return {
        "event_type": "filled",
        "strategy_id": strategy,
        "symbol": "BTCUSDT",
        "side": side,
        "fill_qty": qty,
        "price": price,
        "fill_price": price,
        "fill_time": T0 + timedelta(minutes=minutes),
        **extra,
    }


def _legacy_exit(side, price, minutes, **extra):
    """An OCO exit as tradeengine published it before te#743: the position side in ``side``."""
    return _fill(
        side,
        1.0,
        price,
        minutes,
        reason="oco_exit_take_profit",
        close_reason="take_profit",
        **extra,
    )


def test_exit_shaped_events():
    assert is_exit_shaped({"reduce_only": True})
    assert is_exit_shaped({"payload": {"reduce_only": True}})
    assert is_exit_shaped({"reason": "oco_exit_stop_loss"})
    assert is_exit_shaped({"reason": "cio_exit_now"})
    assert is_exit_shaped({"reason": "cio_scale_out"})
    assert is_exit_shaped({"close_reason": "take_profit"})
    assert is_exit_shaped({"payload": {"position_status": "closed"}})
    assert not is_exit_shaped({"reason": "user_data_stream_fill"})
    assert not is_exit_shaped({})


def test_only_exit_shaped_long_short_rows_are_mapped():
    assert legacy_exit_side({"side": "LONG", "reduce_only": True}) == ("sell", "LONG")
    assert legacy_exit_side({"side": "short", "reason": "oco_exit_x"}) == (
        "buy",
        "SHORT",
    )
    # not exit-shaped: stays unusable
    assert legacy_exit_side({"side": "LONG", "reason": "user_data_stream_fill"}) is None
    assert order_side({"side": "LONG"}) == ("long", False)
    # an ordinary row is unchanged
    assert order_side({"side": "BUY"}) == ("buy", False)
    assert order_side({"side": "SELL", "reduce_only": True}) == ("sell", False)


def test_pnl_calculator_recovers_legacy_exits_with_the_right_sign():
    calc = PnlCalculator()
    calc.apply_fill(_fill("BUY", 1.0, 100.0, 0))
    impact = calc.apply_fill(_legacy_exit("LONG", 110.0, 5))
    assert impact is not None and impact.realized_pnl == pytest.approx(10.0)
    assert calc.legacy_exit_side_mapped == 1

    short = PnlCalculator()
    short.apply_fill(_fill("SELL", 1.0, 100.0, 0))
    impact = short.apply_fill(_legacy_exit("SHORT", 90.0, 5))
    assert impact is not None and impact.realized_pnl == pytest.approx(
        10.0
    )  # short won


def test_pnl_calculator_still_drops_unmappable_sides():
    calc = PnlCalculator()
    assert (
        calc.apply_fill(_fill("LONG", 1.0, 100.0, 0, reason="user_data_stream_fill"))
        is None
    )
    assert calc.legacy_exit_side_mapped == 0


def test_round_book_closes_rounds_on_legacy_exits():
    # bollinger_squeeze_alert: entries carry buy/sell, the OCO exits carried LONG/SHORT
    rows = [
        _fill("BUY", 1.0, 100.0, 0, position_side="LONG"),
        _legacy_exit("LONG", 110.0, 10),  # +10
        _fill("SELL", 1.0, 100.0, 20, position_side="SHORT"),
        _legacy_exit("SHORT", 95.0, 30),  # +5
    ]
    report = build_report(rows, now=NOW)
    stats = report["strategies"]["bollinger_squeeze_alert"]
    assert stats["closed_rounds"] == 2
    assert stats["open_rounds"] == 0
    assert stats["legacy_exit_side_mapped"] == 2
    assert stats["realized_pnl_closed_rounds"] == pytest.approx(15.0)
    assert report["totals"]["legacy_exit_side_mapped"] == 2
    assert report["unattributed"] == {}
    assert report["accounted"] is True


def test_legacy_exit_nets_with_entries_that_carry_no_position_side():
    # every entry before te#743 has no position side (netted); the legacy exit names LONG, but that leg has
    # no open lots, so it nets against the entries and closes the round: this is production history
    rows = [_fill("BUY", 1.0, 100.0, 0), _legacy_exit("LONG", 110.0, 10)]
    report = build_report(rows, now=NOW)
    stats = report["strategies"]["bollinger_squeeze_alert"]
    assert stats["closed_rounds"] == 1
    assert stats["realized_pnl_closed_rounds"] == pytest.approx(10.0)
    assert stats["legacy_exit_side_mapped"] == 1
    assert report["unattributed"] == {}


def test_round_book_without_legacy_rows_reports_zero():
    report = build_report([_fill("BUY", 1.0, 100.0, 0)], now=NOW)
    assert report["totals"]["legacy_exit_side_mapped"] == 0
