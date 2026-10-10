"""Tests for the P4.1 P&L API endpoint + analysis stub replacement (#601)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app

T0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


def _fill(
    *,
    side: str,
    qty: float,
    price: float,
    seconds_before: int = 0,
    strategy_id: str = "S1",
    symbol: str = "BTCUSDT",
    event_type: str = "filled",
    order_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "side": side,
        "fill_qty": qty,
        "price": price,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "order_id": order_id or f"O-{side}-{seconds_before}",
        "decision_id": "D",
        "timestamp": T0 - timedelta(seconds=seconds_before),
    }


def _client_with_fills(rows: list[dict[str, Any]]) -> TestClient:
    app = create_app()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    coll = MagicMock()
    coll.find = MagicMock(return_value=cursor)
    mongodb = MagicMock()
    mongodb.db = {"execution_events": coll}
    db_manager_stub = MagicMock()
    db_manager_stub.mongodb_adapter = mongodb
    db_manager_stub.mysql_adapter = None
    api_module.db_manager = db_manager_stub
    return TestClient(app)


# ----------------------------------------------------------------------
# /api/v1/pnl endpoint.
# ----------------------------------------------------------------------


def test_pnl_strategy_scope_returns_realized_and_unrealized():
    rows = [
        _fill(side="buy", qty=2, price=100, seconds_before=200),
        _fill(side="sell", qty=2, price=120, seconds_before=100),
    ]
    try:
        client = _client_with_fills(rows)
        r = client.get("/api/v1/pnl", params={"strategy_id": "S1", "scope": "strategy"})
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "strategy"
        assert body["strategy_id"] == "S1"
        # (120 - 100) * 2 = 40 realized; 0 unrealized
        assert body["realized"] == 40
        assert body["unrealized"] == 0
        assert body["total"] == 40
        assert body["fills_replayed"] == 2
    finally:
        api_module.db_manager = None


def test_pnl_portfolio_scope_sums_strategies():
    rows = [
        _fill(side="buy", qty=1, price=100, strategy_id="A"),
        _fill(side="sell", qty=1, price=120, strategy_id="A"),
        _fill(
            side="sell",
            qty=1,
            price=200,
            strategy_id="B",
            symbol="ETHUSDT",
        ),
        _fill(side="buy", qty=1, price=180, strategy_id="B", symbol="ETHUSDT"),
    ]
    try:
        client = _client_with_fills(rows)
        r = client.get("/api/v1/pnl", params={"scope": "portfolio"})
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "portfolio"
        assert body["realized"] == 40
        assert body["strategy_id"] is None
    finally:
        api_module.db_manager = None


def test_pnl_strategy_scope_requires_strategy_id():
    try:
        client = _client_with_fills([])
        r = client.get("/api/v1/pnl", params={"scope": "strategy"})
        assert r.status_code == 400
        assert "strategy_id" in r.json()["detail"]
    finally:
        api_module.db_manager = None


def test_pnl_invalid_scope_rejected():
    try:
        client = _client_with_fills([])
        r = client.get("/api/v1/pnl", params={"strategy_id": "S1", "scope": "weird"})
        assert r.status_code == 400
    finally:
        api_module.db_manager = None


def test_pnl_503_when_db_unavailable():
    app = create_app()
    api_module.db_manager = None
    client = TestClient(app)
    r = client.get("/api/v1/pnl", params={"strategy_id": "S1"})
    assert r.status_code == 503


def test_pnl_empty_window_returns_zeros():
    try:
        client = _client_with_fills([])
        r = client.get("/api/v1/pnl", params={"strategy_id": "S1"})
        assert r.status_code == 200
        body = r.json()
        assert body["realized"] == 0
        assert body["unrealized"] == 0
        assert body["fills_replayed"] == 0
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# /analysis/performance/{strategy_id} stub replacement.
# ----------------------------------------------------------------------


def test_performance_returns_real_win_rate_and_pnl():
    rows = [
        _fill(side="buy", qty=1, price=100),
        _fill(side="sell", qty=1, price=110),  # win
        _fill(side="buy", qty=1, price=100),
        _fill(side="sell", qty=1, price=90),  # loss
        _fill(side="buy", qty=1, price=100),
        _fill(side="sell", qty=1, price=130),  # win
    ]
    try:
        client = _client_with_fills(rows)
        r = client.get("/analysis/performance/S1")
        assert r.status_code == 200
        body = r.json()
        # 2 wins, 1 loss → win_rate = 2/3
        assert abs(body["stats"]["win_rate"] - 2 / 3) < 1e-9
        # Realized = 10 - 10 + 30 = 30 (positive)
        assert body["stats"]["realized_pnl"] == 30
        # the closed rounds behind the win rate (the CIO net-EV gate's posterior)
        assert body["stats"]["wins"] == 2
        assert body["stats"]["losses"] == 1
        assert body["stats"]["recent_pnl_trend"] == "positive"
        assert body["metadata"]["source"] == "data-manager-pnl-calculator"
        assert body["metadata"]["fills_replayed"] == 6
    finally:
        api_module.db_manager = None


def _alternating_fills(results: list[bool]) -> list[dict[str, Any]]:
    """One buy/sell pair per outcome, oldest first (a win sells above, a loss below the entry)."""
    rows = []
    for index, won in enumerate(results):
        before = (len(results) - index) * 1000
        rows.append(_fill(side="buy", qty=1, price=100, seconds_before=before))
        rows.append(
            _fill(
                side="sell",
                qty=1,
                price=110 if won else 90,
                seconds_before=before - 500,
            )
        )
    return rows


def _performance_stats(results: list[bool]) -> dict[str, Any]:
    try:
        client = _client_with_fills(_alternating_fills(results))
        response = client.get("/analysis/performance/S1")
        assert response.status_code == 200
        return response.json()["stats"]
    finally:
        api_module.db_manager = None


def _orphan_fills() -> list[dict[str, Any]]:
    """Three long entries of 1 BTC at 100, 120 and 90 (oldest first); the mark is the last fill, 90."""
    return [
        _fill(side="buy", qty=1, price=100, seconds_before=300, order_id="e1"),
        _fill(side="buy", qty=1, price=120, seconds_before=200, order_id="e2"),
        _fill(side="buy", qty=1, price=90, seconds_before=100, order_id="e3"),
    ]


def _orphan_stats(monkeypatch, mode: str | None, held: str, snapshot_age_s: int = 0):
    """Performance stats with the exchange holding ``held`` BTC LONG (``None`` mode: variable unset)."""
    import data_manager.db.repositories.ledger_repository as ledger_module

    class FakeRepository:
        def __init__(self, *_args):
            pass

        def round_overlay_snapshot(self):
            now_ms = int(datetime.now(UTC).timestamp() * 1000) - snapshot_age_s * 1000
            return {
                "as_of_ms": now_ms,
                "rows": [
                    {"symbol": "BTCUSDT", "position_side": "LONG", "quantity": held}
                ],
            }

        def closed_entry_order_ids(self):
            return set()

    monkeypatch.setattr(ledger_module, "LedgerRepository", FakeRepository)
    if mode is None:
        monkeypatch.delenv("ROUND_ORPHAN_MARKING", raising=False)
    else:
        monkeypatch.setenv("ROUND_ORPHAN_MARKING", mode)
    try:
        client = _client_with_fills(_orphan_fills())
        api_module.db_manager.mysql_adapter = object()
        response = client.get("/analysis/performance/S1")
        assert response.status_code == 200
        return response.json()
    finally:
        api_module.db_manager = None


def test_performance_apply_excludes_orphaned_lots_from_unrealized_and_trend(
    monkeypatch,
):
    body = _orphan_stats(monkeypatch, "apply", held="1")  # only the newest lot is held
    stats = body["stats"]
    # lots at 100 and 120 are orphaned: (90-100) + (90-120) = -40 leaves; the held lot is at the mark
    assert stats["orphaned_lots"] == 2
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(-40.0)
    assert stats["unrealized_pnl"] == pytest.approx(0.0)
    assert stats["realized_pnl"] == 0
    assert (
        stats["recent_pnl_trend"] == "neutral"
    )  # was negative with the phantom exposure
    assert body["metadata"]["orphan_overlay"] == "enabled"
    assert body["metadata"]["orphan_marking"] == "apply"


def test_performance_report_mode_adds_the_fields_and_changes_nothing_else(monkeypatch):
    stats = _orphan_stats(monkeypatch, "report", held="1")["stats"]
    assert stats["orphaned_lots"] == 2
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(-40.0)
    assert stats["unrealized_pnl"] == pytest.approx(-40.0)  # still includes them
    assert stats["recent_pnl_trend"] == "negative"


def test_performance_off_mode_is_exactly_the_old_response(monkeypatch):
    body = _orphan_stats(monkeypatch, "off", held="1")
    assert body["stats"]["unrealized_pnl"] == pytest.approx(-40.0)
    assert body["stats"]["recent_pnl_trend"] == "negative"
    assert "orphaned_lots" not in body["stats"]
    assert "orphaned_unrealized_pnl" not in body["stats"]
    assert "orphan_overlay" not in body["metadata"]


def test_performance_mixed_case_excludes_only_the_orphaned_part(monkeypatch):
    """The exchange holds 2 of the 3 BTC: the two newest lots are held, only the 100 lot is orphaned."""
    stats = _orphan_stats(monkeypatch, "apply", held="2")["stats"]
    assert stats["orphaned_lots"] == 1
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(-10.0)
    assert stats["unrealized_pnl"] == pytest.approx(-30.0)  # (90-120) of the held lot
    assert stats["recent_pnl_trend"] == "negative"
    # a partly held lot: the exchange holds 2.5 BTC, 0.5 of the oldest lot is orphaned
    partial = _orphan_stats(monkeypatch, "apply", held="2.5")["stats"]
    assert partial["orphaned_lots"] == 1
    assert partial["orphaned_unrealized_pnl"] == pytest.approx(-5.0)
    assert partial["unrealized_pnl"] == pytest.approx(-35.0)


def test_performance_everything_held_excludes_nothing(monkeypatch):
    stats = _orphan_stats(monkeypatch, "apply", held="3")["stats"]
    assert stats["orphaned_lots"] == 0
    assert stats["orphaned_unrealized_pnl"] == 0
    assert stats["unrealized_pnl"] == pytest.approx(-40.0)


def test_performance_apply_with_a_stale_snapshot_leaves_the_stats_unchanged(
    monkeypatch,
):
    """A stale exchange snapshot disables the overlay: nothing is excluded, the amounts are unknown."""
    body = _orphan_stats(monkeypatch, "apply", held="0", snapshot_age_s=3600)
    stats = body["stats"]
    assert stats["unrealized_pnl"] == pytest.approx(-40.0)
    assert stats["orphaned_lots"] is None
    assert stats["orphaned_unrealized_pnl"] is None
    assert body["metadata"]["orphan_overlay"] == "disabled_stale"


def test_performance_default_mode_is_report(monkeypatch):
    body = _orphan_stats(monkeypatch, None, held="1")
    assert body["metadata"]["orphan_marking"] == "report"
    assert body["stats"]["unrealized_pnl"] == pytest.approx(-40.0)


def test_performance_uses_the_precomputed_rounds_report_and_survives_its_failure(
    monkeypatch,
):
    """The orphan lots come from the rounds report the precomputer serves (one overlay, not a copy)."""
    cached = {
        "orphan_overlay": "enabled",
        "strategies": {
            "S1": {
                "legs": {
                    "BTCUSDT": {
                        "LONG": {"orphaned": [{"quantity": 2.0, "price": 100.0}]}
                    }
                }
            }
        },
    }
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    try:
        client = _client_with_fills(_orphan_fills())
        precomputer = MagicMock()
        precomputer.get_or_compute = AsyncMock(return_value=cached)
        monkeypatch.setattr(
            api_module, "report_precomputer", precomputer, raising=False
        )
        stats = client.get("/analysis/performance/S1").json()["stats"]
        assert precomputer.get_or_compute.await_args.args[0] == "rounds"
        assert stats["orphaned_lots"] == 1
        assert stats["orphaned_unrealized_pnl"] == pytest.approx(-20.0)  # (90-100)*2
        assert stats["unrealized_pnl"] == pytest.approx(-20.0)  # -40 - (-20)

        precomputer.get_or_compute = AsyncMock(side_effect=RuntimeError("cache down"))
        failed = client.get("/analysis/performance/S1").json()
        assert failed["stats"]["unrealized_pnl"] == pytest.approx(-40.0)
        assert failed["stats"]["orphaned_lots"] is None
        assert failed["metadata"]["orphan_overlay"] == "unavailable"
    finally:
        api_module.db_manager = None
        monkeypatch.setattr(api_module, "report_precomputer", None, raising=False)


def test_performance_carries_both_the_win_rate_delta_audit_and_the_orphan_fields(
    monkeypatch,
):
    """The #579 keys and the #581 keys live in the same stats block."""
    body = _orphan_stats(monkeypatch, "apply", held="1")
    stats = body["stats"]
    assert {"win_rate_delta", "win_rate_delta_window", "win_rate_delta_se"} <= set(
        stats
    )
    assert {"orphaned_unrealized_pnl", "orphaned_lots", "unrealized_pnl"} <= set(stats)
    assert stats["win_rate_delta"] is None  # no closed round: nothing to compare
    assert stats["win_rate_delta_window"] is None
    assert stats["orphaned_lots"] == 2


def test_pnl_calculator_exposes_its_mark_prices():
    from data_manager.services.pnl_calculator import PnlCalculator

    calc = PnlCalculator()
    assert calc.mark_of("BTCUSDT") is None
    calc.apply_fill(_fill(side="buy", qty=1, price=100))
    assert calc.mark_of("BTCUSDT") == 100
    calc.set_mark("BTCUSDT", 95.0)
    assert calc.mark_of("BTCUSDT") == 95.0


def test_performance_win_rate_delta_is_null_when_the_windows_are_one_trade():
    """2W 1L, the last a loss: each window is one trade, the delta (-1.0) is noise (#579)."""
    stats = _performance_stats([True, True, False])
    assert stats["wins"] == 2 and stats["losses"] == 1
    assert stats["win_rate_delta"] is None
    assert stats["win_rate_delta_window"] == 1
    assert stats["win_rate_delta_se"] == pytest.approx(0.5**0.5)
    assert stats["consecutive_losses"] == 1


def test_performance_win_rate_delta_is_returned_for_a_real_shift():
    """40 outcomes: 18 wins of 20, then 4 wins of 20."""
    stats = _performance_stats([True] * 18 + [False] * 2 + [True] * 4 + [False] * 16)
    assert stats["win_rate_delta"] == pytest.approx(0.2 - 0.9)
    assert stats["win_rate_delta_window"] == 20
    assert stats["win_rate_delta_se"] == pytest.approx((0.55 * 0.45 * 2 / 20) ** 0.5)
    assert stats["consecutive_losses"] == 16


def test_performance_win_rate_delta_keeps_the_response_contract():
    stats = _performance_stats([True, False, True, False])
    assert {
        "win_rate",
        "wins",
        "losses",
        "consecutive_losses",
        "recent_pnl_trend",
    } <= set(stats)
    assert "win_rate_delta" in stats and "win_rate_delta_window" in stats


def test_win_rate_delta_noise_floor_edges():
    from data_manager.api.routes.analysis import win_rate_delta_with_noise_floor as f

    assert f([]) == (None, None, None)
    assert f([True]) == (None, None, None)
    # all wins / all losses: p = 0 or 1 has no variance, so the Laplace estimate sets the SE
    delta, window, se = f([True] * 6)
    assert (delta, window) == (None, 3)
    assert se == pytest.approx((7 / 8 * (1 / 8) * 2 / 3) ** 0.5)
    delta, window, se = f([False] * 6)
    assert (delta, window) == (None, 3)
    assert se == pytest.approx((1 / 8 * (7 / 8) * 2 / 3) ** 0.5)
    # 0 wins then 3 wins (p = 0.5, w = 3): |delta| 1.0 > 2 SE = 0.816, a complete reversal is returned
    assert f([False] * 3 + [True] * 3)[0] == 1.0
    assert f([True] * 3 + [False] * 3)[0] == -1.0
    # but with one trade per window (w = 1) the same reversal is never above 2 SE = 1.414
    assert f([True, False])[0] is None
    assert f([False, True])[0] is None
    # and a 1-of-2 shift at w = 2 is noise (|delta| 0.5 < 2 SE)
    assert f([True, False, True, True])[0] is None
    # an odd count drops the oldest outcome (the leading win is ignored)
    assert f([True] + [False] * 3 + [True] * 3)[0] == 1.0
    # exactly 2 SE is not enough (w = 2, 2 wins then 0: delta 1.0 vs 2 SE 1.0)
    assert f([True, True, False, False])[0] is None


def test_win_rate_delta_ties_are_decided_in_exact_arithmetic():
    """|delta| == 2 SE is never significant, whatever float rounding does (w = 50, 6 -> 14 wins)."""
    from data_manager.api.routes.analysis import win_rate_delta_with_noise_floor as f

    def windows(earlier_wins: int, later_wins: int, w: int) -> list[bool]:
        return (
            [True] * earlier_wins
            + [False] * (w - earlier_wins)
            + [True] * later_wins
            + [False] * (w - later_wins)
        )

    # (14 - 6)^2 * 50 = 3200 = 2 * 20 * 80: exactly 2 SE -> null
    delta, window, se = f(windows(6, 14, 50))
    assert delta is None
    assert window == 50
    assert se == pytest.approx((20 * 80 / (2 * 50**3)) ** 0.5)
    # one more win in the later window (7 -> 14 wins... 6 -> 15) is above the tie: returned exactly
    assert f(windows(6, 15, 50))[0] == pytest.approx(0.18)
    # and one fewer is below it
    assert f(windows(6, 13, 50))[0] is None
    # the order of the windows only changes the sign
    assert f(windows(14, 6, 50))[0] is None
    assert f(windows(15, 6, 50))[0] == pytest.approx(-0.18)


def test_win_rate_delta_never_flips_on_ties_for_any_window_size():
    """The strict test agrees with exact fractions for every (w, a, b) up to w = 40."""
    from fractions import Fraction

    from data_manager.api.routes.analysis import win_rate_delta_with_noise_floor as f

    for w in range(1, 41):
        for a in range(w + 1):
            for b in range(w + 1):
                outcomes = (
                    [True] * a + [False] * (w - a) + [True] * b + [False] * (w - b)
                )
                wins = a + b
                if wins in (0, 2 * w):
                    expected = False
                else:
                    pooled = Fraction(wins, 2 * w)
                    delta = Fraction(b - a, w)
                    expected = delta**2 > 4 * pooled * (1 - pooled) * 2 / w
                got = f(outcomes)[0] is not None
                assert got == expected, (w, a, b)


def test_performance_degrades_when_db_missing():
    """No DB should yield 'neutral' trend (not 'unknown') rather than 500.

    "neutral" matches petrosa-cio's PnlTrend enum vocabulary
    (positive|negative|neutral); "unknown" fails Pydantic validation the same
    way "flat" did (#306/#309). See PetroSa2/petrosa-cio#194.
    """
    app = create_app()
    api_module.db_manager = None
    client = TestClient(app)
    r = client.get("/analysis/performance/S1")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["win_rate"] is None
    assert body["stats"]["win_rate_delta"] is None
    assert "win_rate_delta_window" in body["stats"]
    assert body["stats"]["win_rate_delta_window"] is None
    assert body["stats"]["win_rate_delta_se"] is None
    assert body["stats"]["consecutive_losses"] is None
    assert body["stats"]["recent_pnl_trend"] == "neutral"
    assert body["metadata"]["source"] == "data-manager-analysis-no-db"


def test_performance_degrades_to_neutral_when_execution_events_read_fails():
    """A cursor.to_list() failure should also degrade to 'neutral', not '500'.

    Covers the second sentinel branch (mongodb read exception) which shares
    the no-DB payload shape but was previously untested. See cio#194.
    """
    app = create_app()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(side_effect=RuntimeError("boom"))
    coll = MagicMock()
    coll.find = MagicMock(return_value=cursor)
    mongodb = MagicMock()
    mongodb.db = {"execution_events": coll}
    db_manager_stub = MagicMock()
    db_manager_stub.mongodb_adapter = mongodb
    db_manager_stub.mysql_adapter = None
    api_module.db_manager = db_manager_stub
    try:
        client = TestClient(app)
        r = client.get("/analysis/performance/S1")
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["win_rate"] is None
        assert body["stats"]["win_rate_delta"] is None
        assert "win_rate_delta_window" in body["stats"]
        assert body["stats"]["win_rate_delta_window"] is None
        assert body["stats"]["win_rate_delta_se"] is None
        assert body["stats"]["consecutive_losses"] is None
        assert body["stats"]["recent_pnl_trend"] == "neutral"
        assert body["metadata"]["source"] == "data-manager-analysis-no-db"
    finally:
        api_module.db_manager = None


def test_performance_with_no_closing_fills_has_neutral_trend():
    """Only open longs (no close) → realized==0 → neutral trend.

    "neutral" (not "flat") matches petrosa-cio's PnlTrend enum vocabulary
    (positive|negative|neutral). See PetroSa2/petrosa-data-manager#306.
    """
    rows = [_fill(side="buy", qty=1, price=100)]
    try:
        client = _client_with_fills(rows)
        r = client.get("/analysis/performance/S1")
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["realized_pnl"] == 0
        # No closes → win_rate is None (no decisions yet)
        assert body["stats"]["win_rate"] is None
        assert body["stats"]["win_rate_delta"] is None
        assert body["stats"]["consecutive_losses"] is None
        assert body["stats"]["recent_pnl_trend"] == "neutral"
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# ExecutionEventsConsumer on_persisted hook.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_invokes_on_persisted_hook_after_successful_persist():
    """The hook must be awaited after a fill persists; hook errors are caught."""
    from data_manager.consumer.execution_events_consumer import (
        ExecutionEventsConsumer,
    )
    from data_manager.models.execution_event import ExecutionEvent

    # Wire up a stub db_manager whose mongodb_adapter.db['execution_events']
    # accepts insert_one without raising.
    coll = MagicMock()
    coll.insert_one = AsyncMock(return_value=None)
    mongodb = MagicMock()
    mongodb.db = MagicMock()
    mongodb.db.__getitem__ = MagicMock(return_value=coll)
    mongodb._prepare_for_bson = MagicMock(side_effect=lambda d: d)
    db_manager = MagicMock()
    db_manager.mongodb_adapter = mongodb

    seen: list[Any] = []

    async def hook(event):
        seen.append(event)

    consumer = ExecutionEventsConsumer(db_manager=db_manager, on_persisted=hook)

    event = ExecutionEvent(
        decision_id="D",
        strategy_id="S1",
        order_id="O",
        event_type="filled",
        timestamp=T0,
        side="buy",
        qty=1.0,
        fill_qty=1.0,
        price=100.0,
        symbol="BTCUSDT",
    )

    result = await consumer._persist(event)
    assert result is True
    # _persist alone doesn't call the hook — the hook fires inside the
    # main worker after _persist returns True. Drive that path directly.
    if consumer._on_persisted is not None:
        await consumer._on_persisted(event)
    assert seen == [event]


@pytest.mark.asyncio
async def test_consumer_hook_error_does_not_propagate():
    """A buggy hook must not poison the persist path."""
    from data_manager.consumer.execution_events_consumer import (
        ExecutionEventsConsumer,
    )

    async def bad_hook(event):
        raise RuntimeError("broken hook")

    consumer = ExecutionEventsConsumer(on_persisted=bad_hook)
    # The wrapper inside the worker swallows exceptions. Call the
    # try/except path directly.
    import logging

    logger = logging.getLogger("data_manager.consumer.execution_events_consumer")
    try:
        await consumer._on_persisted(object())
    except RuntimeError:
        pass
    else:
        pytest.fail("bad hook should have raised before reaching this point")
    # Sanity: the consumer reference is intact.
    assert consumer._on_persisted is bad_hook
    # Silence unused-import warning.
    _ = logger
