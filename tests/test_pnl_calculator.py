"""Tests for the P4.1 PnlCalculator (#601).

Covers FIFO realized P&L (long-only, short-only, mixed), mark-to-market
unrealized P&L, strategy vs. portfolio scope, and edge cases (missing
fields, zero qty, non-fill event types).
"""

from __future__ import annotations

import pytest

from data_manager.services.pnl_calculator import (
    FILL_EVENT_TYPES,
    PnlCalculator,
)


def _fill(
    *,
    side: str,
    qty: float,
    price: float,
    strategy_id: str = "S1",
    symbol: str = "BTCUSDT",
    event_type: str = "filled",
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "side": side,
        "fill_qty": qty,
        "price": price,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "order_id": "O",
        "decision_id": "D",
    }


def test_fill_event_types_constant():
    assert "filled" in FILL_EVENT_TYPES
    assert "partial_fill" in FILL_EVENT_TYPES


# ----------------------------------------------------------------------
# Long-only flow.
# ----------------------------------------------------------------------


def test_long_only_realized_pnl_via_sell():
    calc = PnlCalculator()
    impact_buy = calc.apply_fill(_fill(side="buy", qty=2, price=100))
    impact_sell = calc.apply_fill(_fill(side="sell", qty=2, price=110))

    assert impact_buy is not None and impact_buy.realized_pnl == 0
    assert impact_buy.opened_qty == 2
    assert impact_sell is not None
    # (110 - 100) * 2 = 20
    assert impact_sell.realized_pnl == 20
    assert impact_sell.opened_qty == 0
    assert calc.strategy_pnl("S1").realized == 20
    assert calc.strategy_pnl("S1").unrealized == 0


def test_long_only_partial_close_leaves_open_lot():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=5, price=100))
    impact = calc.apply_fill(_fill(side="sell", qty=2, price=110))
    # (110 - 100) * 2 = 20 realized; 3 left long at 100
    assert impact.realized_pnl == 20
    # Mark to 105: unrealized = (105 - 100) * 3 = 15
    calc.set_mark("BTCUSDT", 105)
    breakdown = calc.strategy_pnl("S1")
    assert breakdown.realized == 20
    assert breakdown.unrealized == 15
    assert breakdown.total == 35


# ----------------------------------------------------------------------
# Short-only flow.
# ----------------------------------------------------------------------


def test_short_only_realized_pnl_via_buy():
    calc = PnlCalculator()
    impact_sell = calc.apply_fill(_fill(side="sell", qty=2, price=100))
    impact_buy = calc.apply_fill(_fill(side="buy", qty=2, price=80))

    assert impact_sell is not None and impact_sell.realized_pnl == 0
    # (100 - 80) * 2 = 40 realized on the close
    assert impact_buy is not None and impact_buy.realized_pnl == 40
    assert calc.strategy_pnl("S1").realized == 40


def test_short_unrealized_mtm():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="sell", qty=3, price=100))
    calc.set_mark("BTCUSDT", 90)
    # 3 short lots at 100; mark at 90 → (100 - 90) * 3 = 30 unrealized profit
    breakdown = calc.strategy_pnl("S1")
    assert breakdown.realized == 0
    assert breakdown.unrealized == 30


# ----------------------------------------------------------------------
# Mixed flow.
# ----------------------------------------------------------------------


def test_mixed_long_then_short_net():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=4, price=100))
    # Sell 5 — closes 4 long lots at 100, opens 1 short at 110.
    impact = calc.apply_fill(_fill(side="sell", qty=5, price=110))
    # Realized = (110 - 100) * 4 = 40
    assert impact.realized_pnl == 40
    assert impact.opened_qty == 1
    # Mark at 90 → short profit = (110 - 90) * 1 = 20 unrealized
    calc.set_mark("BTCUSDT", 90)
    breakdown = calc.strategy_pnl("S1")
    assert breakdown.realized == 40
    assert breakdown.unrealized == 20


def test_fifo_ordering_uses_oldest_lot_first():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=2, price=100))  # lot 1
    calc.apply_fill(_fill(side="buy", qty=2, price=120))  # lot 2
    # Sell 3 — FIFO closes 2 @ 100 then 1 @ 120
    # Realized = (110 - 100) * 2 + (110 - 120) * 1 = 20 - 10 = 10
    impact = calc.apply_fill(_fill(side="sell", qty=3, price=110))
    assert impact.realized_pnl == 10
    # Remaining 1 long lot at 120; mark at 130 → unrealized 10
    calc.set_mark("BTCUSDT", 130)
    breakdown = calc.strategy_pnl("S1")
    assert breakdown.realized == 10
    assert breakdown.unrealized == 10


# ----------------------------------------------------------------------
# Strategy vs. portfolio scope.
# ----------------------------------------------------------------------


def test_portfolio_sums_across_strategies():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=1, price=100, strategy_id="A"))
    calc.apply_fill(_fill(side="sell", qty=1, price=120, strategy_id="A"))
    calc.apply_fill(
        _fill(side="sell", qty=1, price=200, strategy_id="B", symbol="ETHUSDT")
    )
    calc.apply_fill(
        _fill(side="buy", qty=1, price=180, strategy_id="B", symbol="ETHUSDT")
    )
    # A: 20 realized; B: 20 realized
    assert calc.strategy_pnl("A").realized == 20
    assert calc.strategy_pnl("B").realized == 20
    assert calc.portfolio_pnl().realized == 40


def test_strategy_isolation_in_breakdown():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=1, price=100, strategy_id="A"))
    calc.apply_fill(_fill(side="sell", qty=1, price=110, strategy_id="A"))
    calc.apply_fill(_fill(side="buy", qty=1, price=100, strategy_id="B"))
    # B's open lot has no mark — only A's realized counts.
    assert calc.strategy_pnl("A").realized == 10
    assert calc.strategy_pnl("B").realized == 0


# ----------------------------------------------------------------------
# Mark price handling.
# ----------------------------------------------------------------------


def test_open_lot_without_mark_returns_zero_unrealized():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=1, price=100, symbol="NEW"))
    # No set_mark for NEW; latest fill sets the mark already → fresh mark
    # equals lot price → unrealized = 0.
    assert calc.strategy_pnl("S1").unrealized == 0


def test_set_mark_overrides_latest_fill_price():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=1, price=100))
    calc.set_mark("BTCUSDT", 150)
    assert calc.strategy_pnl("S1").unrealized == 50


# ----------------------------------------------------------------------
# Edge cases.
# ----------------------------------------------------------------------


def test_non_fill_event_type_is_ignored():
    calc = PnlCalculator()
    impact = calc.apply_fill(_fill(side="buy", qty=1, price=100, event_type="rejected"))
    assert impact is None
    assert calc.strategy_pnl("S1").realized == 0


def test_missing_fields_silently_returns_none():
    calc = PnlCalculator()
    bad = {"event_type": "filled", "side": "buy"}  # no qty / price / strategy
    assert calc.apply_fill(bad) is None


def test_zero_quantity_ignored():
    calc = PnlCalculator()
    impact = calc.apply_fill(_fill(side="buy", qty=0, price=100))
    assert impact is None


def test_invalid_side_rejected():
    calc = PnlCalculator()
    impact = calc.apply_fill(_fill(side="diagonal", qty=1, price=100))
    assert impact is None


def test_partial_fill_event_counted():
    calc = PnlCalculator()
    impact = calc.apply_fill(
        _fill(side="buy", qty=1, price=100, event_type="partial_fill")
    )
    assert impact is not None
    assert impact.opened_qty == 1


def test_position_summary_lists_open_and_realized():
    calc = PnlCalculator()
    calc.apply_fill(_fill(side="buy", qty=2, price=100, strategy_id="A"))
    calc.apply_fill(_fill(side="sell", qty=1, price=120, strategy_id="A"))
    summary = calc.position_summary()
    assert len(summary) == 1
    row = summary[0]
    assert row["strategy_id"] == "A"
    assert row["long_qty"] == 1
    assert row["short_qty"] == 0
    assert row["realized"] == 20


@pytest.mark.parametrize(
    "qty,price",
    [
        (-1, 100),
        (1, -100),
        (1, 0),
    ],
)
def test_negative_or_zero_values_rejected(qty, price):
    calc = PnlCalculator()
    assert calc.apply_fill(_fill(side="buy", qty=qty, price=price)) is None
