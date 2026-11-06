"""Ground truth v2 from exchange flat-to-flat episodes (dm#844), on recorded fills."""

from __future__ import annotations

import pytest

from data_manager.services.exchange_episodes import (
    build_ground_truth_v2,
    close_kind,
)

T0 = 1_762_430_400_000  # 2026-11-06T12:00Z in ms


def _fill(
    fid,
    order,
    side,
    qty,
    price,
    minute,
    pnl="0",
    fee="0.01",
    ps="BOTH",
    sym="BTCUSDT",
    asset="USDT",
):
    return {
        "id": fid,
        "orderId": order,
        "symbol": sym,
        "side": side,
        "positionSide": ps,
        "price": price,
        "qty": qty,
        "realizedPnl": pnl,
        "commission": fee,
        "commissionAsset": asset,
        "time": T0 + minute * 60_000,
    }


def _report(fills, order_strategy, orders=(), rows=()):
    return build_ground_truth_v2(list(fills), list(orders), order_strategy, list(rows))


def test_one_way_flat_to_flat_episode_is_attributed_with_exact_net() -> None:
    fills = [
        _fill(1, 10, "BUY", "0.4", "100", 0),
        _fill(2, 11, "BUY", "0.6", "101", 1),
        _fill(3, 20, "SELL", "1.0", "103", 5, pnl="2.4", fee="0.02"),
    ]
    r = _report(fills, {"10": "iceberg_detector", "11": "iceberg_detector"})
    (ep,) = r["episodes"]
    assert ep["status"] == "attributed" and ep["strategy_id"] == "iceberg_detector"
    assert ep["direction"] == "LONG"
    assert ep["realized_pnl"] == pytest.approx(2.4)
    assert ep["commission"] == pytest.approx(0.04)
    assert ep["net_pnl"] == pytest.approx(2.36)
    s = r["strategies"]["iceberg_detector"]
    assert (s["episodes"], s["wins"], s["losses"]) == (1, 1, 0)
    assert next(iter(r)) == "unmatched_headline"


def test_sign_flip_fill_is_split_pro_rata_and_flagged() -> None:
    fills = [
        _fill(1, 10, "BUY", "1", "100", 0, fee="0"),
        # sells 3: closes the long 1 (pnl 2) and opens a short 2
        _fill(2, 20, "SELL", "3", "102", 5, pnl="2", fee="0.03"),
        _fill(3, 30, "BUY", "2", "101", 9, pnl="2", fee="0"),
    ]
    r = _report(fills, {"10": "a", "20": "b"})
    first, second = r["episodes"]
    assert first["direction"] == "LONG" and first["split_fill"] is True
    assert first["realized_pnl"] == pytest.approx(2.0)
    assert first["commission"] == pytest.approx(0.01)  # 1/3 of the flip fill's fee
    assert second["direction"] == "SHORT" and second["split_fill"] is True
    assert second["opening_qty"] == pytest.approx(2.0)
    assert second["commission"] == pytest.approx(0.02)
    assert second["strategy_id"] == "b"  # the flip fill's order opened it
    assert second["realized_pnl"] == pytest.approx(2.0)


def test_hedge_mode_walks_each_position_side_separately() -> None:
    fills = [
        _fill(1, 10, "BUY", "1", "100", 0, ps="LONG"),
        _fill(2, 11, "SELL", "1", "100", 1, ps="SHORT"),
        _fill(3, 12, "SELL", "1", "99", 5, pnl="-1", ps="LONG"),
        _fill(4, 13, "BUY", "1", "98", 6, pnl="2", ps="SHORT"),
    ]
    r = _report(fills, {"10": "s1", "11": "s2"})
    by_side = {ep["position_side"]: ep for ep in r["episodes"]}
    assert by_side["LONG"]["strategy_id"] == "s1"
    assert by_side["SHORT"]["strategy_id"] == "s2"
    assert r["strategies"]["s1"]["losses"] == 1 and r["strategies"]["s2"]["wins"] == 1


def test_mixed_opening_strategies_are_multi_strategy_never_split() -> None:
    fills = [
        _fill(1, 10, "BUY", "1", "100", 0),
        _fill(2, 11, "BUY", "3", "100", 1),
        _fill(3, 20, "SELL", "4", "101", 5, pnl="4"),
    ]
    r = _report(fills, {"10": "rsi_extreme_reversal", "11": "spread_liquidity"})
    (ep,) = r["episodes"]
    assert ep["status"] == "multi_strategy" and ep["strategy_id"] is None
    assert ep["strategy_shares"] == {
        "rsi_extreme_reversal": 0.25,
        "spread_liquidity": 0.75,
    }
    assert r["strategies"]["rsi_extreme_reversal"]["episodes"] == 0
    assert r["strategies"]["rsi_extreme_reversal"]["not_attributed_episodes"] == {
        "multi_strategy": 1
    }


def test_opening_fill_without_a_strategy_is_unmapped() -> None:
    fills = [_fill(1, 10, "BUY", "1", "100", 0), _fill(2, 20, "SELL", "1", "100", 5)]
    r = _report(fills, {})
    assert r["episodes"][0]["status"] == "unmapped"
    assert r["unmatched_headline"]["unmapped_opening_orders"] == 1


def test_window_edges_are_not_counted() -> None:
    fills = [
        # closes a position opened before the window
        _fill(1, 5, "SELL", "1", "100", 0, pnl="-3"),
        _fill(2, 10, "BUY", "1", "100", 2),
        _fill(3, 20, "SELL", "1", "101", 3, pnl="1"),
        # still open at the end
        _fill(4, 30, "BUY", "2", "100", 9),
    ]
    r = _report(fills, {"10": "s", "30": "s"})
    statuses = [ep["status"] for ep in r["episodes"]]
    assert statuses == ["started_before_window", "attributed", "open_at_window_end"]
    assert r["strategies"]["s"]["episodes"] == 1
    assert r["unmatched_headline"]["by_status"] == {
        "attributed": 1,
        "open_at_window_end": 1,
        "started_before_window": 1,
    }


@pytest.mark.parametrize(
    ("order", "kind"),
    [
        ({"type": "STOP_MARKET"}, "stop_or_tp_trigger"),
        ({"type": "MARKET", "origType": "TAKE_PROFIT_MARKET"}, "stop_or_tp_trigger"),
        ({"type": "MARKET", "reduceOnly": True}, "reduce_only_market"),
        ({"type": "MARKET", "clientOrderId": "autoclose-123"}, "liquidation"),
        ({"type": "MARKET", "clientOrderId": "adl_autoclose"}, "adl"),
        ({"type": "MARKET"}, "market"),
        ({"type": "LIMIT"}, "limit"),
        (None, "unknown"),
    ],
)
def test_close_kinds(order, kind) -> None:
    assert close_kind(order) == kind


def test_non_usdt_fee_gives_null_net_and_shows_in_reconciliation() -> None:
    fills = [
        _fill(1, 10, "BUY", "1", "100", 0, fee="0.0001", asset="BNB"),
        _fill(2, 20, "SELL", "1", "101", 5, pnl="1", fee="0"),
    ]
    r = _report(fills, {"10": "s"})
    assert r["episodes"][0]["net_pnl"] is None
    assert r["strategies"]["s"]["net_unknown"] == 1
    assert r["reconciliation"]["sum_fill_realized_pnl_minus_commission"] is None


def test_reconciliation_matches_raw_fills() -> None:
    fills = [
        _fill(1, 10, "BUY", "1", "100", 0, fee="0.01"),
        _fill(2, 20, "SELL", "3", "102", 5, pnl="2", fee="0.03"),
        _fill(3, 30, "BUY", "2", "101", 9, pnl="2", fee="0.02"),
    ]
    rec = _report(fills, {"10": "a", "20": "b"})["reconciliation"]
    assert rec["sum_fill_realized_pnl_minus_commission"] == pytest.approx(3.94)
    assert rec["sum_episode_net_pnl"] == pytest.approx(3.94)
    assert rec["gap"] == pytest.approx(0.0)


def test_ledger_rows_annotate_and_external_closes_are_classified() -> None:
    fills = [
        _fill(1, 10, "BUY", "1", "100", 0),
        _fill(2, 20, "SELL", "1", "99", 5, pnl="-1"),
    ]
    orders = [{"orderId": 20, "type": "MARKET", "reduceOnly": True}]
    rows = [
        {
            "strategy_position_id": "sp1",
            "strategy_id": "iceberg_detector",
            "status": "closed_externally",
            "entry_order_id": "10",
            "close_reason": "no_exchange_position",
        },
        # a stale "open" row never blocks anything: it only annotates
        {
            "strategy_position_id": "stale",
            "strategy_id": "iceberg_detector",
            "status": "open",
            "entry_order_id": "999",
        },
    ]
    r = _report(fills, {"10": "iceberg_detector"}, orders, rows)
    ep = r["episodes"][0]
    assert ep["status"] == "attributed"
    assert ep["ledger_rows"][0]["strategy_position_id"] == "sp1"
    assert ep["close_kinds"] == {"reduce_only_market": 1}
    assert r["external_closes"] == {"iceberg_detector": {"reduce_only_market": 1}}
    assert r["strategies"]["iceberg_detector"]["close_kinds"] == {
        "reduce_only_market": 1
    }
