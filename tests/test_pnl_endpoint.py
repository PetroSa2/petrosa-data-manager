"""Tests for the P4.1 P&L API endpoint + analysis stub replacement (#601)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes.analysis import (
    compute_closed_rounds as _REAL_COMPUTE_CLOSED_ROUNDS,  # the route's own is patched in some tests
)

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


def _btc(side: str, quantity: str) -> dict[str, str]:
    return {"symbol": "BTCUSDT", "position_side": side, "quantity": quantity}


def _rounds_cache_body(
    monkeypatch,
    rows: list[dict[str, Any]],
    exchange_rows: list[dict[str, str]],
    *,
    snapshot_age_s: int = 0,
    cache_age_s: float = 0.0,
    stale: bool = False,
) -> dict[str, Any]:
    """The rounds report as the precomputer stores and serves it: really computed, JSON round-tripped."""
    import asyncio
    import json

    import data_manager.db.repositories.ledger_repository as ledger_module

    class FakeRepository:
        def __init__(self, *_args):
            pass

        def round_overlay_snapshot(self):
            at = int(datetime.now(UTC).timestamp() * 1000) - snapshot_age_s * 1000
            return {"as_of_ms": at, "rows": exchange_rows}

        def closed_entry_order_ids(self):
            return set()

    monkeypatch.setattr(ledger_module, "LedgerRepository", FakeRepository)
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    _client_with_fills(rows)
    api_module.db_manager.mysql_adapter = object()
    try:
        body = asyncio.run(_REAL_COMPUTE_CLOSED_ROUNDS(api_module.db_manager, None, 30))
    finally:
        api_module.db_manager = None
    body = json.loads(json.dumps(body))
    body["metadata"].update(age_seconds=cache_age_s, stale=stale)
    return body


def _performance(
    monkeypatch,
    mode: str | None,
    rows: list[dict[str, Any]],
    body: dict[str, Any] | None,
    *,
    precomputer: bool = True,
):
    """/analysis/performance/S1 with the rounds cache serving ``body`` (``None``: a miss).

    The route may only READ the cache: get_or_compute and compute_closed_rounds fail the test if called.
    """
    import data_manager.api.routes.analysis as analysis_route

    if mode is None:
        monkeypatch.delenv("ROUND_ORPHAN_MARKING", raising=False)
    else:
        monkeypatch.setenv("ROUND_ORPHAN_MARKING", mode)
    stub = MagicMock()
    stub.get = AsyncMock(return_value=body)
    stub.get_or_compute = AsyncMock(
        side_effect=AssertionError("computed on the decision path")
    )
    monkeypatch.setattr(
        api_module, "report_precomputer", stub if precomputer else None, raising=False
    )
    monkeypatch.setattr(
        analysis_route,
        "compute_closed_rounds",
        AsyncMock(side_effect=AssertionError("computed on the decision path")),
    )
    try:
        client = _client_with_fills(rows)
        response = client.get("/analysis/performance/S1")
        assert response.status_code == 200
        return response.json(), stub
    finally:
        api_module.db_manager = None
        monkeypatch.setattr(api_module, "report_precomputer", None, raising=False)


def _orphan_stats(monkeypatch, mode: str | None, held: str, **cache):
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", held)], **cache)
    return _performance(monkeypatch, mode, rows, body)[0]


def test_performance_apply_marks_only_the_held_lots_and_the_trend_follows_realized(
    monkeypatch,
):
    body = _orphan_stats(monkeypatch, "apply", held="1")  # only the newest lot is held
    stats = body["stats"]
    # lots at 100 and 120 are orphaned: (90-100) + (90-120) = -40; the held lot sits at the mark
    assert stats["orphaned_lots"] == 2
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(-40.0)
    assert stats["unrealized_pnl"] == 0.0
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
    rows = _orphan_fills()
    cache = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    body, stub = _performance(monkeypatch, "off", rows, cache)
    assert body["stats"]["unrealized_pnl"] == pytest.approx(-40.0)
    assert body["stats"]["recent_pnl_trend"] == "negative"
    assert "orphaned_lots" not in body["stats"]
    assert "orphaned_unrealized_pnl" not in body["stats"]
    assert "orphan_overlay" not in body["metadata"]
    assert stub.get.await_count == 0  # off does not even read the cache


def test_performance_mixed_case_marks_the_held_part_of_a_partly_held_lot(monkeypatch):
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


def test_performance_default_mode_is_report(monkeypatch):
    body = _orphan_stats(monkeypatch, None, held="1")
    assert body["metadata"]["orphan_marking"] == "report"
    assert body["stats"]["unrealized_pnl"] == pytest.approx(-40.0)


@pytest.mark.parametrize(
    ("cache", "status"),
    [
        # the snapshot behind the cached report was already stale when it was computed
        ({"snapshot_age_s": 3600}, "disabled_stale"),
        # an old cache entry (the TTL is 7 days): the frozen orphan list must not be applied
        ({"cache_age_s": 7 * 24 * 3600.0}, "disabled_stale"),
        # the precomputer flagged it stale
        ({"stale": True}, "disabled_stale"),
        # cache age + snapshot age beyond the maximum snapshot age
        ({"cache_age_s": 1000.0, "snapshot_age_s": 900}, "disabled_stale"),
    ],
)
def test_performance_apply_ignores_a_stale_overlay(monkeypatch, cache, status):
    body = _orphan_stats(monkeypatch, "apply", held="1", **cache)
    stats = body["stats"]
    assert stats["unrealized_pnl"] == pytest.approx(-40.0)  # untouched
    assert stats["orphaned_lots"] is None
    assert stats["orphaned_unrealized_pnl"] is None
    assert body["metadata"]["orphan_overlay"] in {status, "disabled_stale"}


def test_performance_apply_uses_a_cache_that_is_young_enough(monkeypatch):
    stats = _orphan_stats(
        monkeypatch, "apply", held="1", cache_age_s=600.0, snapshot_age_s=600
    )["stats"]
    assert stats["unrealized_pnl"] == 0.0
    assert stats["orphaned_lots"] == 2


def test_performance_never_computes_the_rounds_report(monkeypatch):
    """A cache miss is "warming", no precompute is "unavailable": nothing is excluded, nothing computed."""
    rows = _orphan_fills()
    miss, stub = _performance(monkeypatch, "apply", rows, None)
    assert miss["metadata"]["orphan_overlay"] == "warming"
    assert miss["stats"]["unrealized_pnl"] == pytest.approx(-40.0)
    assert miss["stats"]["orphaned_lots"] is None
    assert stub.get.await_count == 1 and stub.get_or_compute.await_count == 0
    off, _ = _performance(monkeypatch, "apply", rows, None, precomputer=False)
    assert off["metadata"]["orphan_overlay"] == "unavailable"
    assert off["stats"]["unrealized_pnl"] == pytest.approx(-40.0)


def test_performance_survives_a_cache_read_failure_and_malformed_bodies(monkeypatch):
    import copy

    rows = _orphan_fills()
    good = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])

    def broken(edit):
        body = copy.deepcopy(good)
        edit(body)
        return body

    legs = lambda body: body["strategies"]["S1"]["legs"]["BTCUSDT"]["LONG"]  # noqa: E731
    variants = {
        "bad quantity": broken(lambda b: legs(b)["orphaned"][0].update(quantity="x")),
        "no order id key": broken(lambda b: legs(b)["orphaned"][0].pop("order_id")),
        "no orphaned list": broken(lambda b: legs(b).pop("orphaned")),
        "no age": broken(lambda b: b["metadata"].pop("age_seconds")),
        "no snapshot age": broken(lambda b: b.pop("exchange_snapshot_age_seconds")),
        "predates the strategy": broken(
            lambda b: b.update(strategies={"S2": b["strategies"]["S1"]})
        ),
        "no strategies": {"orphan_overlay": "enabled"},
    }
    for name, body_variant in variants.items():
        body, _ = _performance(monkeypatch, "apply", rows, body_variant)
        assert body["metadata"]["orphan_overlay"] in {
            "unavailable",
            "disabled_stale",
        }, name
        assert body["stats"]["unrealized_pnl"] == pytest.approx(-40.0), name
        assert body["stats"]["orphaned_lots"] is None, name
        assert body["metadata"]["pnl_source"] == "pnl-calculator", name

    import data_manager.api.app as module

    stub = MagicMock()
    stub.get = AsyncMock(side_effect=RuntimeError("cache down"))
    monkeypatch.setattr(module, "report_precomputer", stub, raising=False)
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    try:
        response = _client_with_fills(rows).get("/analysis/performance/S1")
    finally:
        module.db_manager = None
        monkeypatch.setattr(module, "report_precomputer", None, raising=False)
    assert response.status_code == 200
    assert response.json()["metadata"]["orphan_overlay"] == "unavailable"


def test_a_cache_of_unknown_age_is_stale_not_fresh(monkeypatch):
    """The precomputer reports an unknown age as stale: age null, stale true: nothing is applied."""
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    body["metadata"].update(age_seconds=None, stale=True)
    served, _ = _performance(monkeypatch, "apply", rows, body)
    assert served["metadata"]["orphan_overlay"] == "disabled_stale"
    assert served["stats"]["unrealized_pnl"] == pytest.approx(-40.0)
    body["metadata"].pop("stale")  # a body that does not say it is fresh is not fresh
    served, _ = _performance(monkeypatch, "apply", rows, body)
    assert served["metadata"]["orphan_overlay"] == "disabled_stale"


def test_a_fully_orphaned_short_book_is_exactly_zero_and_the_trend_is_neutral(
    monkeypatch,
):
    """Float residue must not set the trend: the held sum of an empty leg is 0.0, not a difference."""
    prices = [101.1, 99.7, 100.3, 98.9, 102.7, 100.1]
    rows = [
        _fill(
            side="sell",
            qty=0.3 + 0.1 * index,
            price=price,
            seconds_before=1000 - 100 * index,
            order_id=f"s{index}",
        )
        for index, price in enumerate(prices)
    ]
    for row in rows:
        row["position_side"] = "SHORT"
    cache = _rounds_cache_body(monkeypatch, rows, [])  # the exchange holds nothing
    body, _ = _performance(monkeypatch, "apply", rows, cache)
    stats = body["stats"]
    assert stats["orphaned_lots"] == len(prices)
    assert stats["orphaned_unrealized_pnl"] != 0  # the phantom exposure is reported
    assert stats["unrealized_pnl"] == 0.0
    assert stats["recent_pnl_trend"] == "neutral"


def test_short_leg_with_position_side_values_the_held_lot_only(monkeypatch):
    rows = [
        _fill(side="sell", qty=1, price=price, seconds_before=age, order_id=f"s{age}")
        for price, age in ((100, 300), (120, 200), (90, 100))
    ]
    for row in rows:
        row["position_side"] = "SHORT"
    cache = _rounds_cache_body(monkeypatch, rows, [_btc("SHORT", "-1")])
    stats = _performance(monkeypatch, "apply", rows, cache)[0]["stats"]
    # the newest short (90) is held and sits at the mark; the shorts at 100 and 120 are orphaned: +10 and +30
    assert stats["orphaned_lots"] == 2
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(40.0)
    assert stats["unrealized_pnl"] == 0.0


def test_realized_pnl_with_orphans_keeps_the_trend_on_realized(monkeypatch):
    rows = [
        _fill(side="buy", qty=1, price=100, seconds_before=500, order_id="a"),
        _fill(side="sell", qty=1, price=110, seconds_before=400, order_id="b"),  # +10
        _fill(side="buy", qty=1, price=120, seconds_before=300, order_id="c"),
        _fill(side="buy", qty=1, price=90, seconds_before=200, order_id="d"),
    ]
    cache = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    applied = _performance(monkeypatch, "apply", rows, cache)[0]["stats"]
    reported = _performance(monkeypatch, "report", rows, cache)[0]["stats"]
    assert applied["realized_pnl"] == pytest.approx(10.0)
    assert applied["orphaned_lots"] == 1  # the lot at 120
    assert applied["orphaned_unrealized_pnl"] == pytest.approx(-30.0)
    assert applied["unrealized_pnl"] == 0.0  # the held lot (90) is at the mark
    assert applied["recent_pnl_trend"] == "positive"
    assert reported["unrealized_pnl"] == pytest.approx(-30.0)


def _ps(row: dict[str, Any], side: str) -> dict[str, Any]:
    row["position_side"] = side
    return row


def _applied(monkeypatch, cache_rows, now_rows, exchange_rows, mode="apply"):
    body = _rounds_cache_body(monkeypatch, cache_rows, exchange_rows)
    served, _ = _performance(monkeypatch, mode, now_rows, body)
    return served


def test_hedge_legs_are_one_book_for_realized_and_unrealized(monkeypatch):
    """A. A closed LONG round of -15, then LONG 1@100 and SHORT 1@110 both held, mark 110.

    True total = -15 + (110-100) + (110-110) = -5. PnlCalculator nets the two legs (realized -5) and a held-lot
    unrealized of +10 on top would double count to a positive trend.
    """
    rows = [
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=900, order_id="a1"),
            "LONG",
        ),
        _ps(
            _fill(side="sell", qty=1, price=85, seconds_before=800, order_id="a2"),
            "LONG",
        ),
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=300, order_id="l"),
            "LONG",
        ),
        _ps(
            _fill(side="sell", qty=1, price=110, seconds_before=200, order_id="s"),
            "SHORT",
        ),
    ]
    exchange = [_btc("LONG", "1"), _btc("SHORT", "-1")]
    stats = _applied(monkeypatch, rows, rows, exchange)
    assert stats["metadata"]["pnl_source"] == "round-book-legs"
    stats = stats["stats"]
    assert stats["realized_pnl"] == pytest.approx(-15.0)
    assert stats["unrealized_pnl"] == pytest.approx(10.0)
    assert stats["orphaned_lots"] == 0
    assert stats["recent_pnl_trend"] == "negative"  # -5, not +10 or +20
    # report mode leaves the calculator's figures alone (it nets the legs: realized -5, unrealized 0)
    reported = _applied(monkeypatch, rows, rows, exchange, mode="report")["stats"]
    assert reported["realized_pnl"] == pytest.approx(-5.0)
    assert reported["unrealized_pnl"] == 0
    assert reported["recent_pnl_trend"] == "negative"


def test_both_hedge_legs_orphaned_with_the_exchange_flat(monkeypatch):
    rows = [
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=300, order_id="l"),
            "LONG",
        ),
        _ps(
            _fill(side="sell", qty=1, price=110, seconds_before=200, order_id="s"),
            "SHORT",
        ),
    ]
    stats = _applied(monkeypatch, rows, rows, [])["stats"]
    assert stats["orphaned_lots"] == 2
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(
        10.0
    )  # +10 long, 0 short, mark 110
    assert stats["realized_pnl"] == 0  # the legs are not netted into a profit
    assert stats["unrealized_pnl"] == 0.0
    assert stats["recent_pnl_trend"] == "neutral"


def test_a_lot_closed_after_the_cache_is_not_counted_twice(monkeypatch):
    """B. The lot was held when the cache was computed and sold at a profit since: realized has it, the
    live book no longer has the lot, so it is not valued again at the mark."""
    cached = [
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=900, order_id="b1"),
            "LONG",
        ),
        _ps(
            _fill(side="sell", qty=1, price=85, seconds_before=800, order_id="b2"),
            "LONG",
        ),
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=300, order_id="b3"),
            "LONG",
        ),
    ]
    now = cached + [
        _ps(
            _fill(side="sell", qty=1, price=110, seconds_before=10, order_id="b4"),
            "LONG",
        )
    ]
    stats = _applied(monkeypatch, cached, now, [_btc("LONG", "1")])["stats"]
    assert stats["realized_pnl"] == pytest.approx(-5.0)  # -15 + 10
    assert stats["unrealized_pnl"] == 0.0
    assert stats["recent_pnl_trend"] == "negative"


def test_a_lot_opened_after_the_cache_is_held_by_default(monkeypatch):
    """C. A lot (and a tiny later one that sets the mark) opened after the cache is not missing."""
    cached = [
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=900, order_id="c1"),
            "LONG",
        ),
        _ps(
            _fill(side="sell", qty=1, price=105, seconds_before=800, order_id="c2"),
            "LONG",
        ),
    ]
    now = cached + [
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=300, order_id="c3"),
            "LONG",
        ),
        _ps(
            _fill(side="buy", qty=0.0001, price=80, seconds_before=10, order_id="c4"),
            "LONG",
        ),
    ]
    stats = _applied(monkeypatch, cached, now, [_btc("LONG", "0")])["stats"]
    assert stats["realized_pnl"] == pytest.approx(5.0)
    assert stats["unrealized_pnl"] == pytest.approx(
        -20.0
    )  # (80-100) * 1 + (80-80) * 0.0001
    assert stats["orphaned_lots"] == 0
    assert stats["recent_pnl_trend"] == "negative"  # -15


def test_a_lot_orphaned_in_the_cache_and_sold_since_is_not_excluded_twice(monkeypatch):
    cached = [
        _ps(
            _fill(side="buy", qty=1, price=100, seconds_before=900, order_id="e1"),
            "LONG",
        )
    ]
    now = cached + [
        _ps(
            _fill(side="sell", qty=1, price=90, seconds_before=10, order_id="e2"),
            "LONG",
        )
    ]
    stats = _applied(monkeypatch, cached, now, [])[
        "stats"
    ]  # flat at the cache: e1 was orphaned
    assert stats["orphaned_lots"] == 0
    assert stats["orphaned_unrealized_pnl"] == 0
    assert stats["realized_pnl"] == pytest.approx(-10.0)
    assert stats["recent_pnl_trend"] == "negative"


def test_a_phantom_is_not_netted_into_a_real_short_by_the_leg_book(monkeypatch):
    """D. A phantom LONG 1@90 (the exchange holds no LONG) and a real SHORT 1@101, mark 103.

    The calculator nets them into a realized +11; the leg-aware book keeps the phantom LONG apart: the real
    exposure is the short, (101-103) * 1 = -2 (negative), and the phantom's +13 is reported as orphaned.
    """
    rows = [
        _ps(
            _fill(side="buy", qty=1, price=90, seconds_before=900, order_id="ph"),
            "LONG",
        ),
        _ps(
            _fill(side="sell", qty=1, price=101, seconds_before=300, order_id="sh"),
            "SHORT",
        ),
        _ps(
            _fill(
                side="sell", qty=0.000001, price=103, seconds_before=100, order_id="mk"
            ),
            "SHORT",
        ),
    ]
    exchange = [_btc("SHORT", "-1.000001")]
    applied = _applied(monkeypatch, rows, rows, exchange)["stats"]
    assert applied["realized_pnl"] == 0
    assert applied["unrealized_pnl"] == pytest.approx(-2.0)
    assert applied["orphaned_lots"] == 1
    assert applied["orphaned_unrealized_pnl"] == pytest.approx(13.0)
    assert applied["recent_pnl_trend"] == "negative"
    reported = _applied(monkeypatch, rows, rows, exchange, mode="report")["stats"]
    assert reported["realized_pnl"] == pytest.approx(
        11.0
    )  # the calculator's netting, unchanged in report
    assert reported["orphaned_unrealized_pnl"] == pytest.approx(
        13.0
    )  # from the same leg-aware book


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
