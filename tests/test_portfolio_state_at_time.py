"""Tests for the portfolio state-at-time-T query (#604 P4.4).

Covers:
  * ``PortfolioStateService.state_at`` — equity replay matches the
    #602 rules, recent-events slices are bounded and ordered correctly,
    strategy_id scopes the query, open-position inference behaves on
    the filled→closed transition.
  * HTTP route — 200 / 422 / 500 / 503 paths and the ``at`` parameter
    handling.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from data_manager.portfolio.state_service import (
    PortfolioStateAtTime,
    PortfolioStateService,
)

NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
AT_T = NOW + timedelta(hours=1)  # the query timestamp


# ---------------------------------------------------------------------------
# Adapter stub
# ---------------------------------------------------------------------------


def _adapter_with_collections(returns: dict[str, list[dict]]):
    """Stub MongoDB adapter where ``find_filtered`` returns a per-collection list.

    The ``returns`` mapping is keyed by collection name. ``find_filtered``
    inspects the first positional arg (collection name) to pick the right
    response.
    """
    adapter = MagicMock()
    adapter._connected = True

    async def find_filtered(collection, **kwargs):
        # Honor sort_order: newest-first reverses the chronological list.
        docs = list(returns.get(collection, []))
        sort_order = kwargs.get("sort_order", -1)
        # Stored chronological; if newest-first requested, reverse.
        if sort_order == -1:
            docs = list(reversed(docs))
        limit = kwargs.get("limit", 1000)
        return docs[:limit]

    adapter.find_filtered = AsyncMock(side_effect=find_filtered)
    return adapter


def _pnl(kind: str, *, realized=0.0, unrealized=0.0, offset_min=0):
    return {
        "pnl_kind": kind,
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized,
        "timestamp": NOW + timedelta(minutes=offset_min),
    }


def _exec(order_id: str, event_type: str, *, offset_min=0, strategy_id="ta-momentum"):
    return {
        "order_id": order_id,
        "event_type": event_type,
        "strategy_id": strategy_id,
        "symbol": "BTCUSDT",
        "timestamp": NOW + timedelta(minutes=offset_min),
    }


def _decision(decision_id: str, action: str = "execute", *, offset_min=0):
    return {
        "decision_id": decision_id,
        "action": action,
        "strategy_id": "ta-momentum",
        "timestamp": NOW + timedelta(minutes=offset_min),
    }


# ---------------------------------------------------------------------------
# PortfolioStateService.state_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_at_empty_window_yields_zeroes():
    adapter = _adapter_with_collections({})
    svc = PortfolioStateService(mongodb_adapter=adapter)

    result = await svc.state_at(AT_T)
    assert result.events_evaluated == 0
    assert result.cumulative_realized_pnl_usd == 0.0
    assert result.peak_equity_usd == 0.0
    assert result.current_drawdown_pct == 0.0
    assert result.recent_decisions == []
    assert result.open_positions == []


@pytest.mark.asyncio
async def test_state_at_computes_cumulative_realized_and_unrealized():
    pnl_events = [
        _pnl("closed", realized=100.0, offset_min=0),
        _pnl("closed", realized=50.0, offset_min=10),
        _pnl("mark_to_market", unrealized=25.0, offset_min=20),
        _pnl("closed", realized=-30.0, offset_min=30),
        _pnl("mark_to_market", unrealized=10.0, offset_min=40),
    ]
    adapter = _adapter_with_collections({"pnl_events": pnl_events})
    svc = PortfolioStateService(mongodb_adapter=adapter)

    result = await svc.state_at(AT_T)
    # Realized: 100 + 50 - 30 = 120
    # Latest unrealized: 10 (last m2m)
    assert result.cumulative_realized_pnl_usd == pytest.approx(120.0)
    assert result.latest_unrealized_pnl_usd == pytest.approx(10.0)
    assert result.current_equity_usd == pytest.approx(130.0)
    # Peak happens at offset 20 — right after the +25 m2m snap when
    # realized was already 150 (from the two closed events): 150 + 25 = 175.
    assert result.peak_equity_usd == pytest.approx(175.0)
    # Drawdown: (175 - 130) / 175 * 100 ≈ 25.71%
    assert result.current_drawdown_pct == pytest.approx((175 - 130) / 175 * 100)


@pytest.mark.asyncio
async def test_state_at_threads_end_time_filter_to_find_filtered():
    """Every find_filtered call must have ``end=at`` so events after T
    don't leak into the reconstruction."""
    adapter = _adapter_with_collections({"pnl_events": [_pnl("closed", realized=1.0)]})
    svc = PortfolioStateService(mongodb_adapter=adapter)

    await svc.state_at(AT_T)
    # find_filtered called for pnl_events + execution_events + decisions = 3 times.
    assert adapter.find_filtered.await_count == 3
    for call in adapter.find_filtered.call_args_list:
        kwargs = call.kwargs
        assert kwargs["end"] == AT_T


@pytest.mark.asyncio
async def test_state_at_threads_strategy_id_filter():
    """When ``strategy_id`` is supplied, every find_filtered call's
    ``filters`` must include it."""
    adapter = _adapter_with_collections({})
    svc = PortfolioStateService(mongodb_adapter=adapter)

    await svc.state_at(AT_T, strategy_id="ta-momentum")
    for call in adapter.find_filtered.call_args_list:
        filters = call.kwargs["filters"]
        assert filters.get("strategy_id") == "ta-momentum"


@pytest.mark.asyncio
async def test_state_at_strategy_id_none_omits_filter():
    """Without strategy_id, the find_filtered ``filters`` map must be empty
    so the query is portfolio-wide."""
    adapter = _adapter_with_collections({})
    svc = PortfolioStateService(mongodb_adapter=adapter)

    await svc.state_at(AT_T)
    for call in adapter.find_filtered.call_args_list:
        filters = call.kwargs["filters"]
        assert "strategy_id" not in filters


@pytest.mark.asyncio
async def test_state_at_chains_recent_events_oldest_first():
    """The recent_decisions / recent_executions slices must be ordered
    oldest-first within the slice so the operator reads forward through
    the chain."""
    decisions = [
        _decision("d-1", offset_min=0),
        _decision("d-2", offset_min=10),
        _decision("d-3", offset_min=20),
    ]
    executions = [
        _exec("o-1", "placed", offset_min=5),
        _exec("o-1", "filled", offset_min=10),
        _exec("o-2", "placed", offset_min=20),
    ]
    adapter = _adapter_with_collections(
        {"cio_decisions": decisions, "execution_events": executions}
    )
    svc = PortfolioStateService(mongodb_adapter=adapter)

    result = await svc.state_at(AT_T)
    assert [d["decision_id"] for d in result.recent_decisions] == ["d-1", "d-2", "d-3"]
    assert [e["event_type"] for e in result.recent_executions] == [
        "placed",
        "filled",
        "placed",
    ]


@pytest.mark.asyncio
async def test_state_at_infers_open_positions_from_filled_orders():
    """Orders whose latest event_type is filled/partial_fill are 'open';
    those followed by closed/cancelled are NOT in the open list."""
    executions = [
        _exec("o-open", "placed", offset_min=0),
        _exec("o-open", "filled", offset_min=5),
        _exec("o-closed", "placed", offset_min=10),
        _exec("o-closed", "filled", offset_min=15),
        _exec("o-closed", "closed", offset_min=20),
    ]
    adapter = _adapter_with_collections({"execution_events": executions})
    svc = PortfolioStateService(mongodb_adapter=adapter)

    result = await svc.state_at(AT_T)
    open_ids = {p["order_id"] for p in result.open_positions}
    assert open_ids == {"o-open"}


@pytest.mark.asyncio
async def test_state_at_respects_chain_limits():
    """Configurable chain limits truncate the recent slices."""
    decisions = [_decision(f"d-{i}", offset_min=i) for i in range(50)]
    adapter = _adapter_with_collections({"cio_decisions": decisions})
    svc = PortfolioStateService(
        mongodb_adapter=adapter,
        decisions_in_chain=5,
        executions_in_chain=5,
        pnl_events_in_chain=5,
    )

    result = await svc.state_at(AT_T)
    # Newest-5 of 50, then re-ordered oldest-first within the slice.
    assert len(result.recent_decisions) == 5
    assert [d["decision_id"] for d in result.recent_decisions] == [
        "d-45",
        "d-46",
        "d-47",
        "d-48",
        "d-49",
    ]


@pytest.mark.asyncio
async def test_state_at_returns_empty_when_adapter_disconnected():
    adapter = MagicMock()
    adapter._connected = False
    svc = PortfolioStateService(mongodb_adapter=adapter)

    result = await svc.state_at(AT_T)
    assert result.events_evaluated == 0
    assert result.recent_decisions == []


@pytest.mark.asyncio
async def test_state_at_swallows_adapter_errors_per_collection():
    """A failure on one find_filtered call mustn't kill the whole query —
    each collection fetch is independently swallowed to []."""
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(side_effect=RuntimeError("mongo hiccup"))
    svc = PortfolioStateService(mongodb_adapter=adapter)

    result = await svc.state_at(AT_T)
    assert result.events_evaluated == 0
    assert result.recent_decisions == []


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


def _client_with_db(db_manager):
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module
    from data_manager.api.app import create_app

    api_module.db_manager = db_manager
    return TestClient(create_app())


def test_route_503_when_db_missing():
    client = _client_with_db(None)
    r = client.get(
        "/api/v1/portfolio/state-at",
        params={"at": "2026-05-21T12:00:00+00:00"},
    )
    assert r.status_code == 503


def test_route_requires_at_param():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    client = _client_with_db(db)
    r = client.get("/api/v1/portfolio/state-at")
    assert r.status_code == 422


def test_route_returns_state_payload():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    fake = PortfolioStateAtTime(
        at=AT_T,
        strategy_id=None,
        cumulative_realized_pnl_usd=100.0,
        latest_unrealized_pnl_usd=10.0,
        current_equity_usd=110.0,
        peak_equity_usd=120.0,
        current_drawdown_pct=8.33,
        open_positions=[],
        recent_decisions=[],
        recent_executions=[],
        recent_pnl_events=[],
        events_evaluated=5,
    )
    with patch(
        "data_manager.portfolio.state_service.PortfolioStateService.state_at",
        new=AsyncMock(return_value=fake),
    ) as mock_state:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/portfolio/state-at",
            params={"at": "2026-05-21T13:00:00+00:00"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["events_evaluated"] == 5
    assert body["cumulative_realized_pnl_usd"] == 100.0
    mock_state.assert_awaited_once()
    kwargs = mock_state.await_args.kwargs
    assert kwargs["strategy_id"] is None
    args = mock_state.await_args.args
    assert args[0] == AT_T


def test_route_threads_strategy_id():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    fake = PortfolioStateAtTime(
        at=AT_T,
        strategy_id="ta-momentum",
        cumulative_realized_pnl_usd=0.0,
        latest_unrealized_pnl_usd=0.0,
        current_equity_usd=0.0,
        peak_equity_usd=0.0,
        current_drawdown_pct=0.0,
    )
    with patch(
        "data_manager.portfolio.state_service.PortfolioStateService.state_at",
        new=AsyncMock(return_value=fake),
    ) as mock_state:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/portfolio/state-at",
            params={
                "at": "2026-05-21T13:00:00+00:00",
                "strategy_id": "ta-momentum",
            },
        )
    assert r.status_code == 200
    kwargs = mock_state.await_args.kwargs
    assert kwargs["strategy_id"] == "ta-momentum"


def test_route_500_when_service_raises():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    with patch(
        "data_manager.portfolio.state_service.PortfolioStateService.state_at",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/portfolio/state-at",
            params={"at": "2026-05-21T13:00:00+00:00"},
        )
    assert r.status_code == 500
