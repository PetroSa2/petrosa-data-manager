"""Tests for the portfolio drawdown vs characterization envelope (#602 P4.2).

Covers:
  * ``DrawdownService.compute`` — peak/current reconstruction across the
    realized/mark_to_market/aggregate pnl_kind variants, drawdown_pct
    math, no-events early return, breach detection vs envelope, and the
    "unknown pnl_kind" fallback.
  * ``DrawdownBreachPublisher.maybe_publish`` — only fires when
    ``result.breached`` is True; failures don't raise.
  * ``DrawdownScheduler.run_cycle`` — iterates discovered strategies,
    publishes breach for breached results, skips healthy ones, and
    survives per-strategy compute failures.
  * ``GET /api/v1/portfolio/drawdown`` — 503 / 500 / 200 paths, query
    threading.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from data_manager.models.characterization import Characterization
from data_manager.portfolio.drawdown_publisher import DrawdownBreachPublisher
from data_manager.portfolio.drawdown_scheduler import DrawdownScheduler
from data_manager.portfolio.drawdown_service import DrawdownResult, DrawdownService

NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(
    kind: str, *, realized=0.0, unrealized=0.0, offset_s=0, strategy_id="ta-momentum"
):
    return {
        "pnl_kind": kind,
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized,
        "timestamp": NOW + timedelta(seconds=offset_s),
        "strategy_id": strategy_id,
    }


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        return self._docs


def _adapter_with_events(events):
    """Build a stub MongoDB adapter that returns ``events`` for any find()."""
    coll = MagicMock()
    coll.find = MagicMock(return_value=_FakeCursor(events))
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=coll)
    adapter = MagicMock()
    adapter._connected = True
    adapter.db = db
    return adapter, coll


def _char_repo_stub(envelope):
    """A stub characterization_repository with a single envelope."""
    repo = AsyncMock()
    if envelope is None:
        repo.get_latest = AsyncMock(return_value=None)
    else:
        repo.get_latest = AsyncMock(
            return_value=Characterization(
                strategy_id="ta-momentum",
                strategy_version="v1",
                data_window_from=NOW - timedelta(days=30),
                data_window_to=NOW - timedelta(days=1),
                seed=42,
                metrics={"sharpe": 1.2, "win_rate": 0.55, "mean_return": 0.001},
                drawdown_envelope=envelope,
                inputs_hash="abc123",
            )
        )
    return repo


# ---------------------------------------------------------------------------
# DrawdownService.compute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_events_returns_zero_drawdown():
    adapter, _ = _adapter_with_events([])
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.events_evaluated == 0
    assert result.current_drawdown_pct == 0.0
    assert result.breached is False
    assert result.envelope == [5.0, 10.0, 20.0, 30.0]
    assert "no pnl events" in result.reason


@pytest.mark.asyncio
async def test_monotonic_growth_yields_no_drawdown():
    events = [
        _ev("closed", realized=100.0, offset_s=0),
        _ev("closed", realized=50.0, offset_s=10),
        _ev("closed", realized=75.0, offset_s=20),
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.peak_equity_usd == pytest.approx(225.0)
    assert result.current_equity_usd == pytest.approx(225.0)
    assert result.current_drawdown_pct == pytest.approx(0.0)
    assert result.breached is False


@pytest.mark.asyncio
async def test_peak_then_loss_detects_drawdown():
    # Peak at 200, then loss back to 150 → drawdown 25%.
    events = [
        _ev("closed", realized=100.0, offset_s=0),
        _ev("closed", realized=100.0, offset_s=10),
        _ev("closed", realized=-50.0, offset_s=20),
    ]
    adapter, _ = _adapter_with_events(events)
    # Envelope p99 = 20% → 25% drawdown exceeds it.
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.peak_equity_usd == pytest.approx(200.0)
    assert result.current_equity_usd == pytest.approx(150.0)
    assert result.current_drawdown_pct == pytest.approx(25.0)
    assert result.envelope_threshold_pct == pytest.approx(20.0)
    assert result.breach_percentile == "p99"
    assert result.breached is True
    assert "exceeds" in result.reason


@pytest.mark.asyncio
async def test_mark_to_market_snapshots_overwrite_unrealized():
    # Realized closes accumulate; m2m overwrites the unrealized component.
    events = [
        _ev("closed", realized=100.0, offset_s=0),
        _ev("mark_to_market", unrealized=50.0, offset_s=10),  # equity = 100 + 50 = 150
        _ev("mark_to_market", unrealized=20.0, offset_s=20),  # equity = 100 + 20 = 120
        _ev("mark_to_market", unrealized=-30.0, offset_s=30),  # equity = 100 + -30 = 70
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=[5.0, 10.0, 30.0, 60.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.peak_equity_usd == pytest.approx(150.0)
    assert result.current_equity_usd == pytest.approx(70.0)
    # (150 - 70) / 150 = 0.5333... → 53.33%
    assert result.current_drawdown_pct == pytest.approx((150 - 70) / 150 * 100)
    assert result.breached is True  # 53.33% > p99=30%


@pytest.mark.asyncio
async def test_drawdown_clamped_when_peak_never_positive():
    # Strategy is underwater from the first event — peak_equity stays 0,
    # so drawdown_pct must clamp to 0 (no meaningful drawdown when
    # equity never went positive).
    events = [
        _ev("closed", realized=-100.0, offset_s=0),
        _ev("closed", realized=-50.0, offset_s=10),
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.peak_equity_usd == 0.0
    assert result.current_equity_usd == pytest.approx(-150.0)
    assert result.current_drawdown_pct == 0.0
    assert result.breached is False


@pytest.mark.asyncio
async def test_aggregate_pnl_kind_folds_into_realized():
    events = [
        _ev("aggregate", realized=200.0, unrealized=10.0, offset_s=0),
        _ev("closed", realized=-100.0, offset_s=10),
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    # Peak: 200 realized + 10 unrealized = 210
    # Current: (200 - 100) realized + 10 unrealized = 110
    assert result.peak_equity_usd == pytest.approx(210.0)
    assert result.current_equity_usd == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_unknown_pnl_kind_falls_back_to_field_presence():
    events = [
        _ev("weird_new_kind", realized=100.0, offset_s=0),
        _ev("weird_new_kind", unrealized=-30.0, offset_s=10),
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=None)
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.peak_equity_usd == pytest.approx(100.0)
    assert result.current_equity_usd == pytest.approx(70.0)


@pytest.mark.asyncio
async def test_missing_envelope_does_not_breach():
    events = [
        _ev("closed", realized=100.0, offset_s=0),
        _ev("closed", realized=-50.0, offset_s=10),
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=None)
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.envelope_threshold_pct is None
    assert result.breach_percentile is None
    assert result.breached is False
    assert "no envelope available" in result.reason


@pytest.mark.asyncio
async def test_compute_returns_empty_when_adapter_disconnected():
    adapter = MagicMock()
    adapter._connected = False
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.events_evaluated == 0
    assert result.current_drawdown_pct == 0.0


@pytest.mark.asyncio
async def test_short_envelope_falls_back_to_last_index():
    # Only [p50, p90] provided — breach_percentile_index defaults to 2,
    # which is out of range, so service falls back to the last entry
    # (the worst-case percentile available).
    events = [
        _ev("closed", realized=100.0, offset_s=0),
        _ev("closed", realized=-50.0, offset_s=10),  # 50% drawdown
    ]
    adapter, _ = _adapter_with_events(events)
    repo = _char_repo_stub(envelope=[5.0, 10.0])
    svc = DrawdownService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.compute("ta-momentum")
    assert result.envelope_threshold_pct == 10.0
    assert result.breach_percentile == "p[1]"
    assert result.breached is True


# ---------------------------------------------------------------------------
# DrawdownBreachPublisher
# ---------------------------------------------------------------------------


def _result(*, breached: bool, strategy_id="ta-momentum") -> DrawdownResult:
    return DrawdownResult(
        strategy_id=strategy_id,
        current_drawdown_pct=25.0 if breached else 5.0,
        envelope_threshold_pct=20.0,
        breach_percentile="p99",
        breached=breached,
        peak_equity_usd=200.0,
        current_equity_usd=150.0,
        events_evaluated=3,
        timestamp=NOW,
        reason="…",
        envelope=[5.0, 10.0, 20.0, 30.0],
    )


@pytest.mark.asyncio
async def test_publisher_publishes_breach():
    nats = AsyncMock()
    pub = DrawdownBreachPublisher(nats_client=nats)

    published = await pub.maybe_publish(_result(breached=True))
    assert published is True
    nats.publish.assert_awaited_once()
    subject, data = nats.publish.call_args.args
    assert subject == "portfolio.drawdown.breach.ta-momentum"
    body = json.loads(data.decode())
    assert body["breached"] is True
    assert body["strategy_id"] == "ta-momentum"


@pytest.mark.asyncio
async def test_publisher_skips_when_not_breached():
    nats = AsyncMock()
    pub = DrawdownBreachPublisher(nats_client=nats)

    published = await pub.maybe_publish(_result(breached=False))
    assert published is False
    nats.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publisher_swallows_nats_errors():
    nats = AsyncMock()
    nats.publish = AsyncMock(side_effect=RuntimeError("NATS disconnected"))
    pub = DrawdownBreachPublisher(nats_client=nats)
    # Must not raise — scheduler loop relies on this.
    published = await pub.maybe_publish(_result(breached=True))
    assert published is False


# ---------------------------------------------------------------------------
# DrawdownScheduler.run_cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_run_cycle_publishes_breaches_and_skips_healthy():
    adapter, _ = _adapter_with_events(
        [
            _ev("closed", realized=100.0, offset_s=0),
            _ev("closed", realized=-50.0, offset_s=10),
        ]
    )
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    nats = AsyncMock()

    scheduler = DrawdownScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=nats,
        interval_seconds=300,
        strategy_ids=["ta-momentum"],
    )
    results = await scheduler.run_cycle()
    assert len(results) == 1
    assert results[0].breached is True
    nats.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_run_cycle_survives_per_strategy_failure():
    adapter, _ = _adapter_with_events([])
    repo = _char_repo_stub(envelope=[5.0, 10.0, 20.0, 30.0])
    nats = AsyncMock()

    scheduler = DrawdownScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=nats,
        interval_seconds=300,
        strategy_ids=["ok", "explodes"],
    )

    original = scheduler._service.compute

    async def flaky(strategy_id, *, start=None, end=None):
        if strategy_id == "explodes":
            raise RuntimeError("synthetic")
        return await original(strategy_id, start=start, end=end)

    scheduler._service.compute = flaky  # type: ignore[assignment]
    results = await scheduler.run_cycle()
    # Only "ok" yielded a result; "explodes" was skipped.
    assert [r.strategy_id for r in results] == ["ok"]


@pytest.mark.asyncio
async def test_scheduler_falls_back_to_adapter_discovery():
    """When no static list, discovery comes from mongodb.list_all_strategy_ids."""
    adapter, _ = _adapter_with_events([])
    adapter.list_all_strategy_ids = AsyncMock(return_value=["s1", "s2"])
    repo = _char_repo_stub(envelope=None)
    nats = AsyncMock()

    scheduler = DrawdownScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=nats,
        interval_seconds=300,
        strategy_ids=None,
    )
    results = await scheduler.run_cycle()
    assert {r.strategy_id for r in results} == {"s1", "s2"}
    adapter.list_all_strategy_ids.assert_awaited_once()


# ---------------------------------------------------------------------------
# HTTP route — GET /api/v1/portfolio/drawdown
# ---------------------------------------------------------------------------


def _client_with_db(db_manager):
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module
    from data_manager.api.app import create_app

    api_module.db_manager = db_manager
    return TestClient(create_app())


def test_route_returns_503_when_db_manager_missing():
    client = _client_with_db(None)
    r = client.get("/api/v1/portfolio/drawdown", params={"strategy_id": "ta-momentum"})
    assert r.status_code == 503


def test_route_requires_strategy_id():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    client = _client_with_db(db)
    r = client.get("/api/v1/portfolio/drawdown")
    assert r.status_code == 422


def test_route_returns_drawdown_result():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    fake = _result(breached=True)

    with patch(
        "data_manager.portfolio.drawdown_service.DrawdownService.compute",
        new=AsyncMock(return_value=fake),
    ) as mock_compute:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/portfolio/drawdown",
            params={"strategy_id": "ta-momentum"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "ta-momentum"
    assert body["breached"] is True
    assert body["envelope_threshold_pct"] == 20.0
    mock_compute.assert_awaited_once()


def test_route_threads_time_window_through():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    with patch(
        "data_manager.portfolio.drawdown_service.DrawdownService.compute",
        new=AsyncMock(return_value=_result(breached=False)),
    ) as mock_compute:
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/portfolio/drawdown",
            params={
                "strategy_id": "ta-momentum",
                "from": "2026-05-21T11:00:00+00:00",
                "to": "2026-05-21T13:00:00+00:00",
            },
        )
    assert r.status_code == 200
    kwargs = mock_compute.await_args.kwargs
    assert kwargs["start"] == datetime(2026, 5, 21, 11, 0, tzinfo=UTC)
    assert kwargs["end"] == datetime(2026, 5, 21, 13, 0, tzinfo=UTC)


def test_route_500_when_compute_raises():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    with patch(
        "data_manager.portfolio.drawdown_service.DrawdownService.compute",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        client = _client_with_db(db)
        r = client.get(
            "/api/v1/portfolio/drawdown",
            params={"strategy_id": "ta-momentum"},
        )
    assert r.status_code == 500
