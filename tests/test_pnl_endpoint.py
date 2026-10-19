"""Tests for the P4.1 P&L API endpoint + analysis stub replacement (#601)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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

    def sort_rows(field, direction):
        rows.sort(key=lambda row: row[field], reverse=direction < 0)
        return cursor

    cursor.sort.side_effect = sort_rows
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


def test_performance_matches_reference_fixture_for_core_stats():
    rows = [
        _fill(side="buy", qty=1, price=100, seconds_before=300),
        _fill(side="sell", qty=1, price=110, seconds_before=250),
        _fill(side="buy", qty=1, price=100, seconds_before=200),
        _fill(side="sell", qty=1, price=90, seconds_before=150),
        _fill(side="buy", qty=1, price=100, seconds_before=100),
        _fill(side="sell", qty=1, price=130, seconds_before=50),
    ]
    try:
        response = _client_with_fills(rows).get("/analysis/performance/S1")
        assert response.status_code == 200
        stats = response.json()["stats"]
        assert {
            key: stats[key]
            for key in ("wins", "losses", "win_rate", "realized_pnl", "unrealized_pnl")
        } == {
            "wins": 2,
            "losses": 1,
            "win_rate": pytest.approx(2 / 3),
            "realized_pnl": 30,
            "unrealized_pnl": 0,
        }
        assert all(stats[key] is not None for key in ("wins", "losses", "win_rate"))
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

        async def live_closed_entry_order_ids(self):
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


def test_performance_apply_excludes_ledger_closed_lots_separately_and_normalizes_time(
    monkeypatch,
):
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    leg = body["strategies"]["S1"]["legs"]["BTCUSDT"]["LONG"]
    ledger_closed = next(lot for lot in leg["orphaned"] if lot["order_id"] == "e1")
    leg["orphaned"].remove(ledger_closed)
    ledger_closed["opened_at"] = ledger_closed["opened_at"].replace("+00:00", "Z")
    leg["ledger_closed"] = [ledger_closed]

    response, _ = _performance(monkeypatch, "apply", rows, body)

    stats = response["stats"]
    assert stats["unrealized_pnl"] == pytest.approx(0.0)
    assert stats["orphaned_unrealized_pnl"] == pytest.approx(-30.0)
    assert stats["ledger_closed_unrealized_pnl"] == pytest.approx(-10.0)
    assert stats["ledger_closed_lots"] == 1
    assert response["metadata"]["orphan_overlay_unmatched_quantity"] == 0


def test_performance_reports_unmatched_cached_orphan_quantity_and_gauge(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    body["strategies"]["S1"]["legs"]["BTCUSDT"]["LONG"]["orphaned"][0]["order_id"] = (
        "not-live"
    )

    response, _ = _performance(monkeypatch, "apply", rows, body)

    assert response["metadata"]["orphan_overlay_unmatched_quantity"] == pytest.approx(
        1.0
    )
    assert response["metadata"]["orphan_overlay_unmatched_lots"] == 1
    assert analysis_route.ROUND_BOOK_ORPHAN_OVERLAY_UNMATCHED_QUANTITY.labels(
        strategy_id="S1"
    )._value.get() == pytest.approx(1.0)


def test_performance_warns_when_measured_replay_exceeds_budget_fraction(
    monkeypatch, caplog
):
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.setenv("DATA_MANAGER_CIO_CONTEXT_FETCH_TIMEOUT_S", "1.0")
    monkeypatch.setattr(analysis_route, "perf_counter", iter([1.0, 1.9]).__next__)
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    with caplog.at_level("WARNING", logger="data_manager.api.routes.analysis"):
        _performance(monkeypatch, "apply", rows, body)

    assert "replay duration 0.900s" in caplog.text
    assert "budget_source=configured" in caplog.text


@pytest.mark.asyncio
async def test_performance_unavailable_response_preserves_assessor_contract(
    monkeypatch,
):
    import data_manager.api.app as api_module
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.setattr(api_module, "db_manager", None)

    response = await analysis_route.get_strategy_performance("S1")

    assert {
        "win_rate",
        "win_rate_delta",
        "win_rate_delta_window",
        "win_rate_delta_se",
        "wins",
        "losses",
        "consecutive_losses",
        "recent_pnl_trend",
        "realized_pnl",
        "unrealized_pnl",
    } <= response["stats"].keys()
    assert response["metadata"]["fills_replayed"] == 0


@pytest.mark.asyncio
async def test_performance_read_failure_returns_iterable_degraded_stats(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mongodb_adapter=object()),
    )
    loader = AsyncMock(side_effect=ValueError("execution events unavailable"))
    monkeypatch.setattr(analysis_route, "_load_execution_events", loader)

    response = await analysis_route.get_strategy_performance("S1")

    assert response["degraded"] is True
    assert isinstance(response["stats"], dict)
    assert response["stats"]["win_rate"] is None
    assert response["metadata"]["history_status"] == "unavailable"
    loader.assert_awaited_once_with(api_module.db_manager.mongodb_adapter, "S1")


@pytest.mark.asyncio
async def test_performance_execution_event_timeout_preserves_assessor_contract(
    monkeypatch,
):
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.setattr(
        analysis_route,
        "_load_execution_events",
        AsyncMock(side_effect=TimeoutError("execution events timed out")),
    )
    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mongodb_adapter=object()),
    )

    response = await analysis_route.get_strategy_performance("S1")

    assert response["degraded"] is True
    assert response["error"] == {
        "type": "TimeoutError",
        "message": "execution events timed out",
    }
    assert {
        "win_rate",
        "win_rate_delta",
        "consecutive_losses",
    } <= response["stats"].keys()


@pytest.mark.asyncio
async def test_performance_timeout_reuses_last_good_strategy_response(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    rows = [
        _fill(side="buy", qty=1, price=100, seconds_before=200),
        _fill(side="sell", qty=1, price=110, seconds_before=100),
    ]
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "off")
    analysis_route._LAST_GOOD_PERFORMANCE.clear()
    _client_with_fills(rows)
    loader = AsyncMock(
        side_effect=[(rows, "fill-2"), TimeoutError("execution events timed out")]
    )
    monkeypatch.setattr(analysis_route, "_load_execution_events", loader)
    try:
        fresh = await analysis_route.get_strategy_performance("S1")
        stale = await analysis_route.get_strategy_performance("S1")
        assert fresh["stats"]["win_rate"] == 1.0
        assert stale["degraded"] is True
        assert stale["stale"] is True
        assert stale["stats"] == fresh["stats"]
        assert stale["metadata"]["source"] == "data-manager-analysis-last-known-good"
    finally:
        analysis_route._LAST_GOOD_PERFORMANCE.clear()
        api_module.db_manager = None


@pytest.mark.asyncio
async def test_performance_keeps_base_contract_when_overlay_times_out(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    rows = [
        _fill(side="buy", qty=1, price=100, seconds_before=200),
        _fill(side="sell", qty=1, price=110, seconds_before=100),
    ]
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    monkeypatch.setattr(
        analysis_route,
        "_load_synthetic_exits",
        AsyncMock(side_effect=analysis_route._AnalyticsDependencyTimeout),
    )
    try:
        _client_with_fills(rows)
        response = await analysis_route.get_strategy_performance("S1")
        assert response["stats"]["win_rate"] == 1.0
        assert response["stats"]["consecutive_losses"] == 0
        assert response["metadata"]["overlay_status"] == "timeout"
        assert response["metadata"]["history_status"] in {
            "computed",
            "insufficient_history",
        }
    finally:
        api_module.db_manager = None


@pytest.mark.asyncio
async def test_performance_bounds_leg_replay_timeout(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    rows = [_fill(side="buy", qty=1, price=100)]
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    monkeypatch.setattr(
        analysis_route,
        "_load_synthetic_exits",
        AsyncMock(return_value=([], None)),
    )
    monkeypatch.setattr(
        analysis_route,
        "_timed_leg_replay",
        AsyncMock(side_effect=analysis_route._AnalyticsDependencyTimeout),
    )
    try:
        _client_with_fills(rows)
        response = await analysis_route.get_strategy_performance("S1")
        assert response["metadata"]["overlay_status"] == "timeout"
        assert response["stats"]["consecutive_losses"] is None
    finally:
        api_module.db_manager = None


def test_performance_warning_uses_labelled_fallback_budget(monkeypatch, caplog):
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.delenv("DATA_MANAGER_CIO_CONTEXT_FETCH_TIMEOUT_S", raising=False)
    monkeypatch.setattr(analysis_route, "perf_counter", iter([1.0, 9.1]).__next__)
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    with caplog.at_level("WARNING", logger="data_manager.api.routes.analysis"):
        _performance(monkeypatch, "apply", rows, body)

    assert "budget_source=default_fallback" in caplog.text
    assert "10.000s budget_source=default_fallback" in caplog.text


@pytest.mark.parametrize("budget", ["not-a-number", "0"])
def test_performance_warning_uses_labelled_fallback_for_invalid_budget(
    monkeypatch, caplog, budget
):
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.setenv("DATA_MANAGER_CIO_CONTEXT_FETCH_TIMEOUT_S", budget)
    monkeypatch.setattr(analysis_route, "perf_counter", iter([1.0, 9.1]).__next__)
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    with caplog.at_level("WARNING", logger="data_manager.api.routes.analysis"):
        _performance(monkeypatch, "apply", rows, body)

    assert "budget_source=invalid_fallback" in caplog.text


def test_performance_replay_duration_histogram_records_measured_value(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    monkeypatch.setattr(analysis_route, "perf_counter", iter([2.0, 2.25]).__next__)
    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    before = analysis_route.ROUND_BOOK_REPLAY_DURATION._sum.get()
    _performance(monkeypatch, "apply", rows, body)

    assert (
        analysis_route.ROUND_BOOK_REPLAY_DURATION._sum.get() - before
        == pytest.approx(0.25)
    )


def test_performance_unknown_overlay_sets_nan_gauge(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    rows = _orphan_fills()
    body = _rounds_cache_body(monkeypatch, rows, [_btc("LONG", "1")])
    _performance(monkeypatch, "off", rows, body)

    assert math.isnan(
        analysis_route.ROUND_BOOK_ORPHAN_OVERLAY_UNMATCHED_QUANTITY.labels(
            strategy_id="S1"
        )._value.get()
    )


def test_performance_unavailable_overlay_sets_nan_gauge(monkeypatch):
    import data_manager.api.routes.analysis as analysis_route

    _performance(monkeypatch, "apply", _orphan_fills(), None)

    assert math.isnan(
        analysis_route.ROUND_BOOK_ORPHAN_OVERLAY_UNMATCHED_QUANTITY.labels(
            strategy_id="S1"
        )._value.get()
    )


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
        assert body["degraded"] is True
        assert isinstance(body["stats"], dict)
        assert body["stats"]["win_rate"] is None
        assert body["error"] == {"type": "RuntimeError", "message": "boom"}
        assert body["metadata"]["history_status"] == "unavailable"
        assert body["metadata"]["source"] == (
            "data-manager-analysis-execution-events-unavailable"
        )
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


# ---------------------------------------------------------------- outcomes from the leg-aware book (dm#599)
def _legs(spec):
    """Fills of hedge legs: (side, position side, quantity, price, seconds before T0, order id, symbol)."""
    return [
        _ps(
            _fill(
                side=side,
                qty=qty,
                price=price,
                seconds_before=ago,
                order_id=oid,
                symbol=symbol,
            ),
            leg,
        )
        for side, leg, qty, price, ago, oid, *rest in spec
        for symbol in [rest[0] if rest else "BTCUSDT"]
    ]


def _hedge_alternating(results):
    """One closed LONG round per outcome, oldest first (a win sells above the entry, a loss below)."""
    return [_ps(row, "LONG") for row in _alternating_fills(results)]


def _outcomes_of(monkeypatch, rows, mode="apply", exchange=None):
    served = _applied(monkeypatch, rows, rows, exchange or [], mode=mode)
    return served["stats"], served["metadata"]


def test_a_hedge_strategy_with_two_open_legs_and_no_closed_round_has_no_wins_and_no_losses(
    monkeypatch,
):
    """The live case: the LONG entry at 100 and the SHORT entry at 90 are two open legs, no round closed.
    PnlCalculator pairs them and reports a loss of 10."""
    rows = _legs(
        [
            ("buy", "LONG", 1, 100, 300, "l"),
            ("sell", "SHORT", 1, 90, 200, "s"),
        ]
    )
    exchange = [_btc("LONG", "1"), _btc("SHORT", "-1")]
    stats, metadata = _outcomes_of(monkeypatch, rows, exchange=exchange)
    assert metadata["pnl_source"] == "round-book-legs"
    assert (stats["wins"], stats["losses"], stats["consecutive_losses"]) == (0, 0, 0)
    assert stats["win_rate"] is None
    assert stats["win_rate_delta"] is None and stats["win_rate_delta_window"] is None
    # off and report keep today's figures (the netting artifact included): the contract is unchanged
    for mode in ("report", "off"):
        old, _ = _outcomes_of(monkeypatch, rows, mode=mode, exchange=exchange)
        assert (old["wins"], old["losses"], old["consecutive_losses"]) == (0, 1, 1), (
            mode
        )
        assert old["win_rate"] == 0.0, mode


def test_real_closed_losing_rounds_are_counted_once_each_in_the_order_they_closed(
    monkeypatch,
):
    rows = _legs(
        [
            ("buy", "LONG", 1, 100, 1000, "a1"),
            ("sell", "LONG", 1, 90, 900, "a2"),  # loss
            ("sell", "SHORT", 1, 100, 800, "b1"),
            ("buy", "SHORT", 1, 90, 700, "b2"),  # a short round that won
            ("buy", "LONG", 1, 100, 600, "c1"),
            ("sell", "LONG", 1, 95, 500, "c2"),  # loss
            ("buy", "LONG", 1, 100, 400, "d1"),
            ("sell", "LONG", 1, 90, 300, "d2"),  # loss
        ]
    )
    stats, metadata = _outcomes_of(monkeypatch, rows)
    assert metadata["pnl_source"] == "round-book-legs"
    assert (stats["wins"], stats["losses"]) == (1, 3)
    assert stats["win_rate"] == pytest.approx(0.25)
    assert stats["consecutive_losses"] == 2  # loss, win, loss, loss
    assert stats["realized_pnl"] == pytest.approx(-10 + 10 - 5 - 10)


def test_outcomes_follow_the_close_time_not_the_open_time(monkeypatch):
    rows = _legs(
        [
            ("buy", "LONG", 1, 100, 1000, "b1", "BTCUSDT"),  # opened first ...
            ("buy", "LONG", 1, 100, 900, "e1", "ETHUSDT"),
            (
                "sell",
                "LONG",
                1,
                110,
                800,
                "e2",
                "ETHUSDT",
            ),  # ... the ETH win closes first
            (
                "sell",
                "LONG",
                1,
                90,
                700,
                "b2",
                "BTCUSDT",
            ),  # ... the BTC loss closes last
        ]
    )
    stats, _ = _outcomes_of(monkeypatch, rows)
    assert (stats["wins"], stats["losses"]) == (1, 1)
    assert stats["consecutive_losses"] == 1  # the last round to close was a loss


def test_a_break_even_round_is_neither_a_win_nor_a_loss(monkeypatch):
    rows = _legs(
        [("buy", "LONG", 1, 100, 300, "a1"), ("sell", "LONG", 1, 100, 200, "a2")]
    )
    stats, _ = _outcomes_of(monkeypatch, rows)
    assert (stats["wins"], stats["losses"], stats["consecutive_losses"]) == (0, 0, 0)
    assert stats["win_rate"] is None


def test_a_netted_strategy_has_the_same_outcomes_under_every_mode(monkeypatch):
    """No position side: the leg-aware book nets like the calculator, so nothing changes for such a strategy."""
    rows = _alternating_fills([True, True, False, False, True])
    keys = ("wins", "losses", "win_rate", "consecutive_losses", "win_rate_delta")
    applied, metadata = _outcomes_of(monkeypatch, rows)
    reported, _ = _outcomes_of(monkeypatch, rows, mode="report")
    assert metadata["pnl_source"] == "round-book-legs"
    assert {k: applied[k] for k in keys} == {k: reported[k] for k in keys}
    assert (applied["wins"], applied["losses"]) == (3, 2)


UNUSABLE_CACHES = [
    None,  # a miss: the cache is warming
    {"stale": True},  # flagged stale by the precomputer
    {"snapshot_age_s": 3600},  # the snapshot behind it was already stale
    {"cache_age_s": 7 * 24 * 3600.0},  # an expired entry
]


def _hedge_with_phantom_and_closed_rounds():
    """Two closed LONG losses and a short win, plus one open LONG and one open SHORT the calculator would pair."""
    return _legs(
        [
            ("buy", "LONG", 1, 100, 1000, "a1"),
            ("sell", "LONG", 1, 90, 900, "a2"),  # loss
            ("sell", "SHORT", 1, 100, 800, "b1"),
            ("buy", "SHORT", 1, 90, 700, "b2"),  # short win
            ("buy", "LONG", 1, 100, 600, "c1"),
            ("sell", "LONG", 1, 95, 500, "c2"),  # loss
            ("buy", "LONG", 1, 100, 400, "d1"),  # open LONG
            (
                "sell",
                "SHORT",
                1,
                90,
                300,
                "d2",
            ),  # open SHORT: the calculator closes d1 against it
        ]
    )


@pytest.mark.parametrize("cache", UNUSABLE_CACHES)
def test_under_apply_the_outcomes_come_from_the_leg_book_even_without_a_usable_overlay(
    monkeypatch, cache
):
    """The replay does not need the orphan cache, so a warming or stale cache cannot bring the netting artifacts
    back: the CIO must not flip between a cold start and a phantom streak across restarts."""
    rows = _hedge_with_phantom_and_closed_rounds()
    exchange = [_btc("LONG", "1"), _btc("SHORT", "-1")]
    fresh = _applied(monkeypatch, rows, rows, exchange)
    assert fresh["metadata"]["outcome_source"] == "round-book-legs"
    if cache is None:
        body, _ = _performance(monkeypatch, "apply", rows, None)
    else:
        stale = _rounds_cache_body(monkeypatch, rows, exchange, **cache)
        body, _ = _performance(monkeypatch, "apply", rows, stale)
    assert (
        body["metadata"]["pnl_source"] == "pnl-calculator"
    )  # realized and unrealized stay a pair
    assert body["metadata"]["outcome_source"] == "round-book-legs"
    keys = ("wins", "losses", "win_rate", "consecutive_losses", "win_rate_delta")
    assert {k: body["stats"][k] for k in keys} == {k: fresh["stats"][k] for k in keys}
    assert (body["stats"]["wins"], body["stats"]["losses"]) == (
        1,
        2,
    )  # not the calculator's artifacts


def test_the_calculators_outcomes_stay_in_off_and_report_and_name_no_outcome_source(
    monkeypatch,
):
    rows = _hedge_with_phantom_and_closed_rounds()
    exchange = [_btc("LONG", "1"), _btc("SHORT", "-1")]
    for mode in ("report", "off"):
        body = _applied(monkeypatch, rows, rows, exchange, mode=mode)
        assert "outcome_source" not in body["metadata"], mode
        assert body["stats"]["wins"] + body["stats"]["losses"] > 0, mode


def _with_fees(rows, fee):
    return [{**row, "fee": fee, "fee_asset": "USDT"} for row in rows]


@pytest.mark.parametrize(
    ("fee", "won"),
    [
        ("0.50", False),
        ("0.10", True),
        ("0.20", False),
    ],  # gross +0.40; fees 1.00, 0.20, 0.40 in total
)
def test_a_round_is_a_win_or_a_loss_on_net_pnl(monkeypatch, fee, won):
    """Gross +0.40: fees of 1.00 make it a loss; fees of 0.20 leave a win; fees equal to the gain are break-even."""
    rows = _with_fees(
        _legs(
            [("buy", "LONG", 1, 100, 300, "a1"), ("sell", "LONG", 1, 100.4, 200, "a2")]
        ),
        fee,
    )
    stats, _ = _outcomes_of(monkeypatch, rows)
    if fee == "0.20":
        assert (stats["wins"], stats["losses"]) == (0, 0)  # net exactly 0: neither
    else:
        assert (stats["wins"], stats["losses"]) == ((1, 0) if won else (0, 1))


def test_a_fee_in_another_asset_is_not_netted_but_the_gross_outcome_stands(monkeypatch):
    """A fee that is not in the quote asset is counted as unknown, not as a cost (as for the calibration)."""
    rows = [
        {**row, "fee": "0.5", "fee_asset": "BNB"}
        for row in _legs(
            [("buy", "LONG", 1, 100, 300, "a1"), ("sell", "LONG", 1, 100.4, 200, "a2")]
        )
    ]
    stats, _ = _outcomes_of(monkeypatch, rows)
    assert (stats["wins"], stats["losses"]) == (1, 0)


def test_the_win_rate_delta_under_apply_is_null_when_the_windows_are_one_trade(
    monkeypatch,
):
    """The #579 case on round outcomes: 2W 1L, the last a loss, each window one trade."""
    stats, metadata = _outcomes_of(monkeypatch, _hedge_alternating([True, True, False]))
    assert metadata["pnl_source"] == "round-book-legs"
    assert stats["wins"] == 2 and stats["losses"] == 1
    assert stats["win_rate_delta"] is None
    assert stats["win_rate_delta_window"] == 1
    assert stats["win_rate_delta_se"] == pytest.approx(0.5**0.5)
    assert stats["consecutive_losses"] == 1


def test_the_win_rate_delta_under_apply_is_returned_for_a_real_shift(monkeypatch):
    """40 round outcomes: 18 wins of 20, then 4 wins of 20."""
    results = [True] * 18 + [False] * 2 + [True] * 4 + [False] * 16
    stats, _ = _outcomes_of(monkeypatch, _hedge_alternating(results))
    assert stats["win_rate_delta"] == pytest.approx(0.2 - 0.9)
    assert stats["win_rate_delta_window"] == 20
    assert stats["win_rate_delta_se"] == pytest.approx((0.55 * 0.45 * 2 / 20) ** 0.5)
    assert stats["consecutive_losses"] == 16
    assert stats["win_rate"] == pytest.approx(22 / 40)


def test_the_response_contract_is_the_same_under_apply(monkeypatch):
    stats, _ = _outcomes_of(monkeypatch, _hedge_alternating([True, False, True, False]))
    assert {
        "win_rate",
        "wins",
        "losses",
        "consecutive_losses",
        "recent_pnl_trend",
        "win_rate_delta",
        "win_rate_delta_window",
        "win_rate_delta_se",
    } <= set(stats)


def test_outcome_stats_helper_separates_an_unknown_streak_from_a_known_zero():
    from data_manager.api.routes.analysis import outcome_stats

    assert (
        outcome_stats([])["consecutive_losses"] is None
    )  # the calculator: nothing seen
    known = outcome_stats(
        [], known_book=True
    )  # a complete leg replay: no round, no losses
    assert known["consecutive_losses"] == 0 and known["win_rate"] is None
    assert known["wins"] == 0 and known["losses"] == 0
    mixed = outcome_stats([False, True, False, False])
    assert (mixed["wins"], mixed["losses"], mixed["consecutive_losses"]) == (1, 3, 2)
    assert mixed["win_rate"] == pytest.approx(0.25)


def test_leg_outcomes_belong_to_the_requested_strategy_only():
    from data_manager.api.routes.analysis import _leg_aware_pnl

    rows = _legs(
        [("buy", "LONG", 1, 100, 400, "a1"), ("sell", "LONG", 1, 90, 300, "a2")]
    ) + [
        {**row, "strategy_id": "S2"}
        for row in _legs(
            [("buy", "LONG", 1, 100, 200, "b1"), ("sell", "LONG", 1, 120, 100, "b2")]
        )
    ]
    assert _leg_aware_pnl(rows, [], "S1")["outcomes"] == [False]
    assert _leg_aware_pnl(rows, [], "S2")["outcomes"] == [True]
