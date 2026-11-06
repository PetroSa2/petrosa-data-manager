"""Ground truth of closed rounds from exchange fills (dm#844), on recorded fills."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data_manager.services.exchange_ground_truth import build_ground_truth

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _ms(minutes: float) -> int:
    return int((T0 + timedelta(minutes=minutes)).timestamp() * 1000)


def _fill(fid, order_id, side, qty, price, minutes, pnl="0", fee="0.01", **extra):
    """A Binance USD-M userTrades row as the #833 route projects it."""
    return {
        "id": fid,
        "orderId": order_id,
        "symbol": extra.pop("symbol", "BTCUSDT"),
        "side": side,
        "positionSide": extra.pop("positionSide", "BOTH"),
        "price": price,
        "qty": qty,
        "realizedPnl": pnl,
        "commission": fee,
        "commissionAsset": extra.pop("asset", "USDT"),
        "time": _ms(minutes),
    }


def _row(spid, strategy, side="LONG", entry=1, status="closed", **extra):
    return {
        "strategy_position_id": spid,
        "strategy_id": strategy,
        "symbol": extra.pop("symbol", "BTCUSDT"),
        "side": side,
        "status": status,
        "entry_order_id": str(entry),
        "entry_time": T0 + timedelta(minutes=extra.pop("entry_min", 0)),
        "exit_time": extra.pop("exit_time", None),
        **extra,
    }


def test_round_from_its_entry_and_exit_orders() -> None:
    fills = [
        _fill(1, 1, "BUY", "0.004", "100000", 0, fee="0.04"),
        _fill(2, 1, "BUY", "0.006", "100500", 0.1, fee="0.06"),
        _fill(3, 7, "SELL", "0.010", "101000", 30, pnl="7.0", fee="0.10"),
    ]
    orders = [{"orderId": 7, "type": "TAKE_PROFIT_MARKET", "reduceOnly": True}]
    rows = [_row("sp1", "iceberg_detector", tp_order_id="7")]

    gt = build_ground_truth(fills, orders, rows, order_strategy={})

    (r,) = gt["rounds"]
    assert r["attribution"] == "exit_order"
    assert r["entry_price"] == pytest.approx(100300.0)  # VWAP of the two entry fills
    assert r["exit_price"] == pytest.approx(101000.0)
    assert r["gross_pnl"] == pytest.approx(7.0)
    assert r["fees"] == pytest.approx(0.20)
    assert r["net_pnl"] == pytest.approx(6.80)
    assert r["exchange_realized_pnl"] == pytest.approx(7.0)
    assert r["exit_order_types"] == ["TAKE_PROFIT_MARKET"]
    s = gt["strategies"]["iceberg_detector"]
    assert (s["closed_rounds"], s["wins"], s["losses"]) == (1, 1, 0)
    assert gt["totals"]["fills_unmatched"] == 0


def test_short_stop_loss_is_a_loss() -> None:
    fills = [
        _fill(1, 1, "SELL", "1", "100", 0, fee="0"),
        _fill(2, 2, "BUY", "1", "103", 5, pnl="-3", fee="0"),
    ]
    rows = [_row("sp1", "mean_reversion_scalper", side="SHORT", sl_order_id="2")]
    gt = build_ground_truth(fills, [], rows, {})
    assert gt["rounds"][0]["net_pnl"] == pytest.approx(-3.0)
    assert gt["strategies"]["mean_reversion_scalper"]["losses"] == 1


def test_exit_orders_from_the_journal_are_used() -> None:
    fills = [_fill(1, 1, "BUY", "1", "100", 0), _fill(2, 9, "SELL", "1", "101", 5)]
    rows = [_row("sp1", "s")]
    gt = build_ground_truth(fills, [], rows, {}, exit_orders_by_position={"sp1": {"9"}})
    assert gt["rounds"][0]["exit_fill_ids"] == [2]


def test_closed_externally_row_attributed_by_exact_quantity() -> None:
    fills = [
        _fill(1, 1, "BUY", "0.5", "100", 0),
        _fill(2, 50, "SELL", "0.2", "102", 10),
        _fill(3, 51, "SELL", "0.3", "104", 20),
    ]
    rows = [
        _row(
            "ghost",
            "iceberg_detector",
            status="closed_externally",
            close_reason="no_exchange_position",
            exit_time=T0 + timedelta(minutes=25),
        )
    ]
    gt = build_ground_truth(fills, [], rows, {})
    (r,) = gt["rounds"]
    assert r["attribution"] == "closing_fills_by_quantity"
    assert r["exit_price"] == pytest.approx(103.2)
    assert gt["unmatched_rounds"] == []


def test_closed_externally_shared_side_is_unmatched_with_candidates() -> None:
    fills = [
        _fill(1, 1, "BUY", "1", "100", 0),
        _fill(2, 2, "BUY", "1", "100", 1),
        _fill(3, 60, "SELL", "2", "99", 30, pnl="-2"),
    ]
    rows = [
        _row("a", "rsi_extreme_reversal", entry=1, status="closed_externally"),
        _row(
            "b",
            "rsi_extreme_reversal",
            entry=2,
            entry_min=1,
            status="closed_externally",
        ),
    ]
    gt = build_ground_truth(fills, [], rows, {})
    assert gt["rounds"] == []
    reasons = {u["strategy_position_id"]: u["reason"] for u in gt["unmatched_rounds"]}
    assert reasons == {"a": "symbol_side_shared", "b": "symbol_side_shared"}
    assert gt["unmatched_rounds"][0]["candidate_fill_ids"] == [3]
    # Nothing is dropped: the entries are tied to their rows, the shared close listed.
    listed = {
        f["fill_id"]: (f["reason"], f["strategy_position_id"])
        for f in gt["unmatched_fills"]
    }
    assert listed == {
        1: ("entry_of_unmatched_round", "a"),
        2: ("entry_of_unmatched_round", "b"),
        3: ("fill_without_position_row", None),
    }
    assert gt["strategies"]["rsi_extreme_reversal"]["unmatched_rounds"] == 2


@pytest.mark.parametrize(
    ("fills", "reason"),
    [
        ([_fill(1, 1, "BUY", "1", "100", 0)], "no_closing_fills"),
        (
            [_fill(1, 1, "BUY", "1", "100", 0), _fill(2, 5, "SELL", "0.4", "101", 3)],
            "quantity_mismatch",
        ),
    ],
)
def test_unprovable_external_closes_name_the_reason(fills, reason) -> None:
    rows = [_row("g", "s", status="closed_externally")]
    gt = build_ground_truth(fills, [], rows, {})
    assert gt["unmatched_rounds"][0]["reason"] == reason


def test_entry_fills_outside_the_history_are_unmatched() -> None:
    gt = build_ground_truth([], [], [_row("old", "s", entry=404)], {})
    assert gt["unmatched_rounds"][0]["reason"] == "entry_fills_not_found"


def test_fills_without_a_row_are_listed_with_strategy_and_reason() -> None:
    fills = [
        _fill(1, 70, "SELL", "1", "100", 0, pnl="-1"),
        _fill(2, 71, "BUY", "1", "1", 1),
    ]
    orders = [
        {"orderId": 70, "type": "MARKET", "reduceOnly": True},
        {"orderId": 71, "type": "MARKET", "reduceOnly": False},
    ]
    gt = build_ground_truth(
        fills, orders, [], order_strategy={"70": "iceberg_detector"}
    )
    reasons = {
        f["fill_id"]: (f["reason"], f["strategy_id"]) for f in gt["unmatched_fills"]
    }
    assert reasons == {
        1: ("close_without_position_row", "iceberg_detector"),
        2: ("fill_without_position_row", None),
    }


def test_blended_exchange_pnl_differs_from_the_rounds_own_pnl() -> None:
    """Binance's realizedPnl uses the account's blended entry; the round uses its own."""
    fills = [
        _fill(1, 1, "BUY", "1", "100", 0, fee="0"),
        _fill(2, 8, "SELL", "1", "101", 5, pnl="-1", fee="0"),  # blended entry was 102
    ]
    rows = [_row("sp", "inside_bar_breakout", exit_order_id="8")]
    r = build_ground_truth(fills, [], rows, {})["rounds"][0]
    assert r["net_pnl"] == pytest.approx(1.0)
    assert r["exchange_realized_pnl"] == pytest.approx(-1.0)


def test_streaks_open_rows_and_non_usdt_fees() -> None:
    fills = []
    rows = []
    for i, (exit_price, minutes) in enumerate(
        [(99, 10), (98, 20), (101, 30), (97, 40)]
    ):
        entry_id, exit_id = 100 + i, 200 + i
        fills += [
            _fill(entry_id, entry_id, "BUY", "1", "100", minutes - 5, fee="0"),
            _fill(exit_id, exit_id, "SELL", "1", str(exit_price), minutes, fee="0"),
        ]
        rows.append(
            _row(
                f"r{i}",
                "s",
                entry=entry_id,
                entry_min=minutes - 5,
                exit_order_id=str(exit_id),
            )
        )
    fills += [
        _fill(300, 300, "BUY", "1", "100", 50, fee="0.001", asset="BNB"),
        _fill(301, 301, "SELL", "1", "101", 55, fee="0"),
    ]
    rows.append(_row("bnb", "s", entry=300, entry_min=50, exit_order_id="301"))
    rows.append(_row("open", "s", entry=999, status="open"))

    gt = build_ground_truth(fills, [], rows, {})

    s = gt["strategies"]["s"]
    assert (s["closed_rounds"], s["wins"], s["losses"]) == (5, 1, 3)
    assert s["net_unknown"] == 1  # BNB fee: no conversion invented
    assert s["max_consecutive_losses"] == 2
    assert gt["totals"]["open_rows"] == 1
    assert gt["totals"]["fills_in_rounds"] + gt["totals"]["fills_unmatched"] == len(
        fills
    )
