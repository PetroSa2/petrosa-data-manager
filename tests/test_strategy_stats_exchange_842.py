"""Per-strategy stats from the exchange ground truth, rule A (petrosa-data-manager#842)."""

from __future__ import annotations

import pytest

from data_manager.services.strategy_stats_exchange import build_strategy_stats

DAY_MS = 86_400_000
T0 = 1_791_000_000_000


def _episode(
    n, status, net, *, strategy=None, shares=None, open_d=0.0, close_d=1.0, fills=2
):
    return {
        "episode": n,
        "symbol": "BTCUSDT",
        "status": status,
        "strategy_id": strategy,
        "strategy_shares": shares or ({strategy: 1.0} if strategy else {}),
        "open_time_ms": T0 + int(open_d * DAY_MS),
        "close_time_ms": None
        if status == "open_at_window_end"
        else T0 + int(close_d * DAY_MS),
        "realized_pnl": None if net is None else net + 0.1,
        "commission": 0.1,
        "net_pnl": net,
        "fill_ids": list(range(fills)),
    }


GROUND_TRUTH = {
    "episodes": [
        # iceberg alone: one win, one loss
        _episode(1, "attributed", 10.0, strategy="iceberg", close_d=1),
        _episode(2, "attributed", -5.0, strategy="iceberg", close_d=2),
        # a shared position, 96 % iceberg / 4 % surge, a big loss
        _episode(
            3,
            "multi_strategy",
            -200.0,
            shares={"iceberg": 0.96, "surge": 0.04},
            close_d=3,
        ),
        # surge alone wins small
        _episode(4, "attributed", 1.0, strategy="surge", close_d=4),
        # excluded from W/L and P&L
        _episode(5, "open_at_window_end", None, shares={"iceberg": 1.0}, open_d=5),
        _episode(6, "started_before_window", -64.0, close_d=1),
        # a non-USDT fee: net unknown
        _episode(7, "attributed", None, strategy="surge", close_d=5),
    ]
}


@pytest.fixture
def stats():
    return build_strategy_stats(GROUND_TRUTH, window_days=30)


def test_shared_losses_count_by_share_and_outcome_for_every_participant(stats):
    iceberg = stats["iceberg"]["performance"]
    # P&L: 10 - 5 + 0.96 x (-200); attributed-only counting would have shown +5 and hidden the loss
    assert iceberg["net_pnl"] == pytest.approx(10 - 5 - 192)
    assert (iceberg["wins"], iceberg["losses"]) == (
        1,
        2,
    )  # the shared loss counts fully
    surge = stats["surge"]["performance"]
    # the 4 % participant carries 4 % of the loss, not the whole -200
    assert surge["net_pnl"] == pytest.approx(-8 + 1)
    assert (surge["wins"], surge["losses"]) == (1, 1)
    assert stats["iceberg"]["shared_episodes"] == 1


def test_win_rate_streak_and_trend_follow_close_order(stats):
    iceberg = stats["iceberg"]["performance"]
    assert iceberg["win_rate"] == pytest.approx(1 / 3)
    assert (
        iceberg["consecutive_losses"] == 2
    )  # loss on day 2, then the shared loss on day 3
    assert iceberg["recent_pnl_trend"] == "negative"
    assert (
        stats["surge"]["performance"]["consecutive_losses"] == 0
    )  # its last episode won


def test_open_and_pre_window_episodes_are_left_out_and_unknown_nets_are_counted(stats):
    rounds = stats["iceberg"]["rounds"]
    assert rounds["closed_rounds"] == 3
    assert rounds["open_rounds"] == 1
    assert rounds["oldest_open_round_opened_at"] is not None
    assert stats["surge"]["performance"]["net_unknown"] == 1
    assert (
        stats["surge"]["rounds"]["closed_rounds"] == 2
    )  # the unknown one is not a round outcome
    assert None not in stats  # the pre-window episode has no strategy and is dropped


def test_rounds_contract_fields(stats):
    rounds = stats["iceberg"]["rounds"]
    assert rounds["wins"] == 1 and rounds["losses"] == 2
    assert rounds["closed_round_rate_per_day"] == pytest.approx(3 / 30)
    assert rounds["median_holding_seconds"] == pytest.approx(
        2 * 86_400.0
    )  # held 1, 2 and 3 days
    assert rounds["fills"] == 6
    assert rounds["first_fill_at"].startswith("2026-")
    assert rounds["last_closed_at"] > rounds["first_fill_at"]
    assert rounds["realized_pnl_closed_rounds"] == pytest.approx(
        10.1 - 4.9 + 0.96 * -199.9
    )


def test_no_closed_episode_means_unknown_not_zero():
    only_open = {
        "episodes": [_episode(1, "open_at_window_end", None, shares={"x": 1.0})]
    }
    stats = build_strategy_stats(only_open, window_days=30)
    perf = stats["x"]["performance"]
    assert perf["win_rate"] is None
    assert perf["consecutive_losses"] is None
    assert perf["net_pnl"] is None and perf["realized_pnl"] is None
    assert stats["x"]["rounds"]["closed_rounds"] == 0
    assert stats["x"]["rounds"]["median_holding_seconds"] is None


def test_empty_ground_truth_gives_no_strategies():
    assert build_strategy_stats({"episodes": []}, window_days=30) == {}
    assert build_strategy_stats({}, window_days=30) == {}


# --- net R for CIO's keep/kill (strategy-net-r contract) --------------------------------------

# CIO's keep/kill judges a strategy only with at least this many rounds of known R
# (cio/core/keep_kill.py DERIVED_MIN_ROUNDS).
CIO_DERIVED_MIN_ROUNDS = 10


def _row(strategy, *, qty=1.0, entry=100.0, stop=98.0, order="o1"):
    return {
        "strategy_id": strategy,
        "entry_order_id": order,
        "entry_quantity": qty,
        "entry_price": entry,
        "stop_loss_price": stop,
    }


def _risky(n, net, strategy="s", rows=None, shares=None, status="attributed"):
    ep = _episode(
        n,
        status,
        net,
        strategy=strategy if status == "attributed" else None,
        shares=shares,
        close_d=n,
    )
    ep["ledger_rows"] = rows if rows is not None else [_row(strategy)]
    return ep


def test_net_r_is_net_over_the_risk_at_entry():
    # risk = 1 x 100 x 2 % = 2; net -4 -> -2 R; net +3 -> +1.5 R
    gt = {"episodes": [_risky(1, -4.0), _risky(2, 3.0)]}
    block = build_strategy_stats(gt, window_days=30)["s"]["net_r"]
    assert [float(r) for r in block["net_r"]] == pytest.approx([-2.0, 1.5])
    assert block["closed_rounds"] == 2 and block["rounds_without_risk"] == 0
    assert float(block["cumulative_net_loss_usd"]) == pytest.approx(1.0)


def test_a_shared_episode_uses_only_the_strategys_own_rows_and_share():
    rows = [_row("a", qty=1.0, stop=98.0), _row("b", qty=3.0, stop=99.0, order="o2")]
    ep = _risky(
        1, -10.0, rows=rows, shares={"a": 0.25, "b": 0.75}, status="multi_strategy"
    )
    stats = build_strategy_stats({"episodes": [ep]}, window_days=30)
    # a: -2.5 / (1 x 100 x 2 %) = -1.25 R; b: -7.5 / (3 x 100 x 1 %) = -2.5 R
    assert float(stats["a"]["net_r"]["net_r"][0]) == pytest.approx(-1.25)
    assert float(stats["b"]["net_r"]["net_r"][0]) == pytest.approx(-2.5)


@pytest.mark.parametrize(
    "rows",
    [
        [],  # no linked row
        [_row("s", stop=None)],  # no stop
        [_row("s", stop=100.0)],  # stop at entry
        [
            _row("s"),
            _row("s", qty=None, order="o2"),
        ],  # one of two rows unusable: partial risk would overstate R
    ],
)
def test_unknown_risk_is_counted_never_zero(rows):
    block = build_strategy_stats(
        {"episodes": [_risky(1, -4.0, rows=rows)]}, window_days=30
    )["s"]["net_r"]
    assert block["net_r"] == []
    assert block["rounds_without_risk"] == 1
    assert block["unknown_r_share"] == 1.0
    assert float(block["cumulative_net_loss_usd"]) == pytest.approx(
        4.0
    )  # the loss still counts


def test_mostly_unknown_r_is_too_few_rounds_for_keep_kill_never_keep():
    """25 closed episodes, 20 without a stop: CIO sees 5 judged rounds (< its minimum), i.e. insufficient history."""
    episodes = [_risky(i, 1.0) for i in range(1, 6)]  # 5 known, all winners
    episodes += [_risky(i, -1.0, rows=[_row("s", stop=None)]) for i in range(6, 26)]
    block = build_strategy_stats({"episodes": episodes}, window_days=30)["s"]["net_r"]
    assert len(block["net_r"]) == 5 < CIO_DERIVED_MIN_ROUNDS
    assert block["closed_rounds"] == 25
    assert block["rounds_without_risk"] == 20
    assert block["unknown_r_share"] == pytest.approx(0.8)
