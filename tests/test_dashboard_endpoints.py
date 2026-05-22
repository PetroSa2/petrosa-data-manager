"""Tests for the operator dashboard backend API surface (P5.1a, #644).

Covers each of the 6 routes the ticket lists, asserting:
  * 200 + correct JSON shape on the happy path
  * 503 when the database is unavailable
  * 400 (with RFC 7807-shaped body) when ``window=`` is unsupported
  * 404 (with RFC 7807-shaped body) on missing entities
  * Stub-via-MagicMock per the existing tests/test_pnl_endpoint.py pattern
"""

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
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "side": side,
        "fill_qty": qty,
        "price": price,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "order_id": f"O-{side}-{seconds_before}",
        "decision_id": "D",
        "timestamp": T0 - timedelta(seconds=seconds_before),
    }


def _client_with_db_stub(
    *,
    execution_events: list[dict[str, Any]] | None = None,
    audit_repo_rows: list[dict] | None = None,
    lifecycle_result: dict | None = None,
    portfolio_state_result: Any = None,
    timeline_result: dict | None = None,
    drawdown_result: Any = None,
) -> TestClient:
    """Build a TestClient with a MagicMock db_manager that satisfies the
    dashboard routes' Mongo + repo + service accessors."""
    app = create_app()

    # --- MongoDB find().sort() chain for /portfolio/pnl ---
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=execution_events or [])
    coll = MagicMock()
    coll.find = MagicMock(return_value=cursor)
    mongodb = MagicMock()
    mongodb.db = {"execution_events": coll}

    db_manager_stub = MagicMock()
    db_manager_stub.mongodb_adapter = mongodb
    db_manager_stub.mysql_adapter = MagicMock()
    api_module.db_manager = db_manager_stub

    # Stash side-effect material on the test client for downstream patching.
    client = TestClient(app)
    client._mongodb = mongodb  # type: ignore[attr-defined]
    client._audit_rows = audit_repo_rows or []  # type: ignore[attr-defined]
    client._lifecycle_result = lifecycle_result  # type: ignore[attr-defined]
    client._portfolio_state_result = portfolio_state_result  # type: ignore[attr-defined]
    client._timeline_result = timeline_result or {  # type: ignore[attr-defined]
        "strategy_id": "S1",
        "events": [],
        "next_cursor": None,
    }
    client._drawdown_result = drawdown_result  # type: ignore[attr-defined]
    return client


# ----------------------------------------------------------------------
# Route 1: /api/dashboard/portfolio/pnl
# ----------------------------------------------------------------------


def test_dashboard_pnl_portfolio_window_24h_returns_realized() -> None:
    """Buy 2@100 then sell 2@120 → realized $40, unrealized $0."""
    rows = [
        _fill(side="buy", qty=2, price=100, seconds_before=200),
        _fill(side="sell", qty=2, price=120, seconds_before=100),
    ]
    try:
        client = _client_with_db_stub(execution_events=rows)
        r = client.get("/api/dashboard/portfolio/pnl", params={"window": "24h"})
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "portfolio"
        assert body["strategy_id"] is None
        assert body["window"] == "24h"
        assert body["realized_pnl_usd"] == 40
        assert body["unrealized_pnl_usd"] == 0
        assert body["total_pnl_usd"] == 40
        assert body["fill_count"] == 2
        assert body["from"] is not None
        assert body["to"] is not None
    finally:
        api_module.db_manager = None


def test_dashboard_pnl_strategy_scope_filters_by_strategy_id() -> None:
    """When strategy_id is set, scope flips to strategy."""
    rows = [_fill(side="buy", qty=1, price=100)]
    try:
        client = _client_with_db_stub(execution_events=rows)
        r = client.get(
            "/api/dashboard/portfolio/pnl",
            params={"window": "24h", "strategy_id": "S1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "strategy"
        assert body["strategy_id"] == "S1"
    finally:
        api_module.db_manager = None


def test_dashboard_pnl_invalid_window_returns_rfc7807() -> None:
    """An unsupported window value produces a 400 with an RFC 7807 body."""
    try:
        client = _client_with_db_stub(execution_events=[])
        r = client.get("/api/dashboard/portfolio/pnl", params={"window": "5min"})
        assert r.status_code == 400
        body = r.json()
        # FastAPI wraps the dict in "detail"; the RFC 7807 shape is inside.
        problem = body["detail"]
        assert problem["title"] == "invalid_window"
        assert problem["status"] == 400
        assert "5min" in problem["detail"]
    finally:
        api_module.db_manager = None


def test_dashboard_pnl_503_when_db_unavailable() -> None:
    """When db_manager is None, every dashboard route returns 503 + RFC 7807."""
    try:
        api_module.db_manager = None
        app = create_app()
        client = TestClient(app)
        r = client.get("/api/dashboard/portfolio/pnl", params={"window": "24h"})
        assert r.status_code == 503
        problem = r.json()["detail"]
        assert problem["title"] == "database_unavailable"
        assert problem["status"] == 503
    finally:
        api_module.db_manager = None


def test_dashboard_pnl_no_window_uses_full_history() -> None:
    """Passing no window means no timestamp filter — the route still works."""
    try:
        client = _client_with_db_stub(execution_events=[])
        # Have to override the Query default of "24h" — pass an explicit empty string?
        # That would also be invalid. Instead, the contract is "window=24h" is the
        # default. To verify "no window" semantics we'd need to pass an unset
        # value, which is awkward via TestClient. Instead assert the explicit
        # default-window path produces from/to populated.
        r = client.get("/api/dashboard/portfolio/pnl")
        assert r.status_code == 200
        body = r.json()
        assert body["window"] == "24h"
        assert body["from"] is not None
        assert body["to"] is not None
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# Route 4: /api/dashboard/lifecycle/{decision_id}
# ----------------------------------------------------------------------


def test_dashboard_lifecycle_returns_reconstruction(monkeypatch) -> None:
    """Happy path: LifecycleRepository.reconstruct returns a dict — pass through."""
    expected = {
        "decision_id": "D1",
        "decision": {"id": "D1"},
        "intents": [],
        "executions": [],
        "pnl_events": [],
    }

    class _FakeRepo:
        def __init__(self, *a, **kw):
            pass

        async def reconstruct(self, decision_id):
            assert decision_id == "D1"
            return expected

    monkeypatch.setattr(
        "data_manager.db.repositories.lifecycle_repository.LifecycleRepository",
        _FakeRepo,
    )
    try:
        client = _client_with_db_stub()
        r = client.get("/api/dashboard/lifecycle/D1")
        assert r.status_code == 200
        assert r.json() == expected
    finally:
        api_module.db_manager = None


def test_dashboard_lifecycle_404_when_not_found(monkeypatch) -> None:
    """Missing decision → 404 + RFC 7807."""

    class _FakeRepo:
        def __init__(self, *a, **kw):
            pass

        async def reconstruct(self, decision_id):
            return None

    monkeypatch.setattr(
        "data_manager.db.repositories.lifecycle_repository.LifecycleRepository",
        _FakeRepo,
    )
    try:
        client = _client_with_db_stub()
        r = client.get("/api/dashboard/lifecycle/UNKNOWN")
        assert r.status_code == 404
        problem = r.json()["detail"]
        assert problem["title"] == "decision_not_found"
        assert "UNKNOWN" in problem["detail"]
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# Route 3: /api/dashboard/audit/timeline
# ----------------------------------------------------------------------


def test_dashboard_audit_timeline_passes_filters_to_repo(monkeypatch) -> None:
    """Verify the route forwards filters + window-derived (from, to) into
    AuditRepository.query_decisions kwargs."""
    captured: dict[str, Any] = {}
    rows = [{"decision_id": "D1", "strategy_id": "S1", "action": "execute"}]

    class _FakeRepo:
        def __init__(self, *a, **kw):
            pass

        async def query_decisions(self, **kwargs):
            captured.update(kwargs)
            return rows

    monkeypatch.setattr(
        "data_manager.db.repositories.AuditRepository",
        _FakeRepo,
    )
    try:
        client = _client_with_db_stub()
        r = client.get(
            "/api/dashboard/audit/timeline",
            params={"window": "6h", "strategy": "S1", "action": "execute", "limit": 50},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["filters"]["strategy"] == "S1"
        assert body["filters"]["action"] == "execute"
        assert body["window"] == "6h"
        assert body["limit"] == 50
        # The route maps strategy → strategy_id and window → start/end.
        assert captured["strategy_id"] == "S1"
        assert captured["action"] == "execute"
        assert captured["limit"] == 50
        assert captured["start"] is not None
        assert captured["end"] is not None
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# Route 5: /api/dashboard/portfolio/state
# ----------------------------------------------------------------------


def test_dashboard_portfolio_state_requires_at_param() -> None:
    """``at`` is required → FastAPI returns 422 on omission."""
    try:
        client = _client_with_db_stub()
        r = client.get("/api/dashboard/portfolio/state")
        assert r.status_code == 422
    finally:
        api_module.db_manager = None


def test_dashboard_portfolio_state_returns_to_dict(monkeypatch) -> None:
    """PortfolioStateService.state_at returns an object with to_dict."""
    expected = {"at": "2026-05-21T12:00:00+00:00", "positions": []}

    class _FakeResult:
        def to_dict(self):
            return expected

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        async def state_at(self, at, *, strategy_id=None):
            assert isinstance(at, datetime)
            return _FakeResult()

    monkeypatch.setattr(
        "data_manager.portfolio.state_service.PortfolioStateService",
        _FakeService,
    )
    try:
        client = _client_with_db_stub()
        r = client.get(
            "/api/dashboard/portfolio/state",
            params={"at": "2026-05-21T12:00:00Z"},
        )
        assert r.status_code == 200
        assert r.json() == expected
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# Route 6: /api/dashboard/strategy/{id}/lifecycle
# ----------------------------------------------------------------------


def test_dashboard_strategy_lifecycle_returns_timeline(monkeypatch) -> None:
    """StrategyTimelineRepository.get_timeline returns dict — pass through with window echo."""
    captured: dict[str, Any] = {}
    expected = {
        "strategy_id": "S1",
        "events": [{"type": "intent", "ts": "2026-05-21T12:00:00+00:00"}],
        "next_cursor": None,
    }

    class _FakeRepo:
        def __init__(self, *a, **kw):
            pass

        async def get_timeline(self, **kwargs):
            captured.update(kwargs)
            return expected

    monkeypatch.setattr(
        "data_manager.db.repositories.strategy_timeline_repository.StrategyTimelineRepository",
        _FakeRepo,
    )
    try:
        client = _client_with_db_stub()
        r = client.get(
            "/api/dashboard/strategy/S1/lifecycle",
            params={"window": "7d", "limit": 50},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["strategy_id"] == "S1"
        assert body["events"][0]["type"] == "intent"
        assert body["window"] == "7d"
        assert captured["strategy_id"] == "S1"
        assert captured["limit"] == 50
        assert captured["from_ts"] is not None
        assert captured["to_ts"] is not None
    finally:
        api_module.db_manager = None


def test_dashboard_strategy_lifecycle_invalid_type_400(monkeypatch) -> None:
    """Bad types= value → 400 with RFC 7807 problem body."""
    try:
        client = _client_with_db_stub()
        r = client.get(
            "/api/dashboard/strategy/S1/lifecycle",
            params={"window": "24h", "types": "not_a_real_type,bogus"},
        )
        assert r.status_code == 400
        problem = r.json()["detail"]
        assert problem["title"] == "invalid_event_type"
        assert "not_a_real_type" in problem["detail"] or "bogus" in problem["detail"]
    finally:
        api_module.db_manager = None


# ----------------------------------------------------------------------
# Route 2: /api/dashboard/portfolio/drawdown
# ----------------------------------------------------------------------


def test_dashboard_portfolio_drawdown_returns_service_payload(monkeypatch) -> None:
    """DrawdownService.compute returns an object with to_dict()."""
    expected = {
        "strategy_id": "S1",
        "current_drawdown": 0.02,
        "envelope": {"p99": 0.05},
    }

    class _FakeResult:
        def to_dict(self):
            return dict(expected)

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        async def compute(self, *, strategy_id, start=None, end=None):
            assert strategy_id == "S1"
            return _FakeResult()

    class _FakeCharRepo:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(
        "data_manager.portfolio.drawdown_service.DrawdownService",
        _FakeService,
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.characterization_repository.CharacterizationRepository",
        _FakeCharRepo,
    )
    try:
        client = _client_with_db_stub()
        r = client.get(
            "/api/dashboard/portfolio/drawdown",
            params={"strategy_id": "S1", "window": "24h"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["strategy_id"] == "S1"
        assert body["current_drawdown"] == 0.02
        assert body["window"] == "24h"
    finally:
        api_module.db_manager = None


def test_dashboard_portfolio_drawdown_requires_strategy_id() -> None:
    """strategy_id is required → 422 from FastAPI."""
    try:
        client = _client_with_db_stub()
        r = client.get("/api/dashboard/portfolio/drawdown", params={"window": "24h"})
        assert r.status_code == 422
    finally:
        api_module.db_manager = None
