"""Tests for the strategy-fidelity evaluator (#594 P2.3).

Covers:
  * ``FidelityService.evaluate`` — UNKNOWN when no characterization,
    UNKNOWN when sample size below threshold, HEALTHY/UNHEALTHY math,
    NaN/infinity guards, threshold parameterization.
  * ``FidelityVerdictPublisher`` hysteresis — same-verdict ticks
    don't republish; ``stable_ticks_required`` consecutive raw
    observations needed before flipping; publish failure rolls back
    hysteresis so retry works on next tick.
  * ``FidelityScheduler.run_cycle`` — iterates strategies, routes
    results through the publisher, survives per-strategy failures.
  * HTTP route — 200 / 503 / 500 paths.
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
from data_manager.strategies.fidelity_publisher import FidelityVerdictPublisher
from data_manager.strategies.fidelity_scheduler import FidelityScheduler
from data_manager.strategies.fidelity_service import (
    HEALTHY,
    UNHEALTHY,
    UNKNOWN,
    FidelityResult,
    FidelityService,
)

NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _char_repo_stub(mean_return: float | None):
    repo = AsyncMock()
    if mean_return is None:
        repo.get_latest = AsyncMock(return_value=None)
    else:
        repo.get_latest = AsyncMock(
            return_value=Characterization(
                strategy_id="ta-momentum",
                strategy_version="v1",
                data_window_from=NOW - timedelta(days=30),
                data_window_to=NOW - timedelta(days=1),
                seed=42,
                metrics={
                    "sharpe": 1.2,
                    "win_rate": 0.55,
                    "mean_return": mean_return,
                },
                drawdown_envelope=[5.0, 10.0, 20.0, 30.0],
                inputs_hash="abc123",
            )
        )
    return repo


def _adapter_with_pnl(events):
    """Stub MongoDB adapter where find_filtered returns ``events``."""
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(return_value=events)
    return adapter


def _pnl_event(realized: float, *, kind: str = "closed"):
    return {
        "pnl_kind": kind,
        "realized_pnl_usd": realized,
        "timestamp": NOW,
    }


# ---------------------------------------------------------------------------
# FidelityService.evaluate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_characterization_returns_unknown():
    adapter = _adapter_with_pnl([_pnl_event(1.0) for _ in range(20)])
    repo = _char_repo_stub(mean_return=None)
    svc = FidelityService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.evaluate("ta-momentum")
    assert result.verdict == UNKNOWN
    assert "no characterization" in result.reason
    # find_filtered is not even called when there's no characterization.
    adapter.find_filtered.assert_not_called()


@pytest.mark.asyncio
async def test_too_few_samples_returns_unknown():
    adapter = _adapter_with_pnl([_pnl_event(1.0) for _ in range(3)])
    repo = _char_repo_stub(mean_return=1.0)
    svc = FidelityService(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        min_samples=10,
    )

    result = await svc.evaluate("ta-momentum")
    assert result.verdict == UNKNOWN
    assert "need ≥10" in result.reason
    assert result.samples == 3


@pytest.mark.asyncio
async def test_healthy_when_within_threshold():
    # Live mean = 1.0, characterized = 1.1 → divergence ≈ 0.09 < 0.5
    adapter = _adapter_with_pnl([_pnl_event(1.0) for _ in range(15)])
    repo = _char_repo_stub(mean_return=1.1)
    svc = FidelityService(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        threshold=0.5,
    )

    result = await svc.evaluate("ta-momentum")
    assert result.verdict == HEALTHY
    assert result.divergence is not None
    assert result.divergence < 0.5
    assert result.samples == 15


@pytest.mark.asyncio
async def test_unhealthy_when_above_threshold():
    # Live mean = -1.0, characterized = 1.0 → divergence = 2.0 > 0.5
    adapter = _adapter_with_pnl([_pnl_event(-1.0) for _ in range(15)])
    repo = _char_repo_stub(mean_return=1.0)
    svc = FidelityService(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        threshold=0.5,
    )

    result = await svc.evaluate("ta-momentum")
    assert result.verdict == UNHEALTHY
    assert result.divergence == pytest.approx(2.0)
    assert "exceeds threshold" in result.reason


@pytest.mark.asyncio
async def test_zero_characterized_mean_uses_epsilon_denominator():
    """When characterized mean_return is 0, the relative-divergence
    formula must not divide by zero — the service uses a small epsilon
    so the verdict is still computable (and almost always UNHEALTHY when
    live is non-zero)."""
    adapter = _adapter_with_pnl([_pnl_event(1.0) for _ in range(15)])
    repo = _char_repo_stub(mean_return=0.0)
    svc = FidelityService(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        threshold=0.5,
    )

    result = await svc.evaluate("ta-momentum")
    # |1.0 - 0.0| / max(0.0, 1e-9) = 1e9 → huge divergence → unhealthy.
    assert result.verdict == UNHEALTHY
    assert result.divergence is not None
    assert result.divergence > 0.5


@pytest.mark.asyncio
async def test_threshold_is_configurable():
    """A tighter threshold flips the verdict from HEALTHY to UNHEALTHY
    for the same input."""
    adapter = _adapter_with_pnl([_pnl_event(0.5) for _ in range(15)])
    repo = _char_repo_stub(mean_return=1.0)

    loose = await FidelityService(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        threshold=1.0,
    ).evaluate("ta-momentum")
    tight = await FidelityService(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        threshold=0.1,
    ).evaluate("ta-momentum")

    assert loose.verdict == HEALTHY  # 0.5 ≤ 1.0 → fine
    assert tight.verdict == UNHEALTHY  # 0.5 > 0.1 → flag


@pytest.mark.asyncio
async def test_disconnected_adapter_yields_zero_samples():
    adapter = MagicMock()
    adapter._connected = False
    repo = _char_repo_stub(mean_return=1.0)
    svc = FidelityService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.evaluate("ta-momentum")
    assert result.samples == 0
    assert result.verdict == UNKNOWN


@pytest.mark.asyncio
async def test_pnl_fetch_error_is_swallowed_to_empty_samples():
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(side_effect=RuntimeError("mongo hiccup"))
    repo = _char_repo_stub(mean_return=1.0)
    svc = FidelityService(mongodb_adapter=adapter, characterization_repository=repo)

    result = await svc.evaluate("ta-momentum")
    assert result.samples == 0
    assert result.verdict == UNKNOWN


# ---------------------------------------------------------------------------
# FidelityVerdictPublisher hysteresis
# ---------------------------------------------------------------------------


def _result(strategy_id: str, verdict: str, *, reason: str = "ok") -> FidelityResult:
    return FidelityResult(
        strategy_id=strategy_id,
        verdict=verdict,
        reason=reason,
        live_mean_return=1.0,
        characterized_mean_return=1.0,
        divergence=0.0,
        threshold=0.5,
        samples=15,
        timestamp=NOW,
    )


@pytest.mark.asyncio
async def test_publisher_emits_on_first_stable_streak():
    """With stable_ticks_required=2, two consecutive raw HEALTHY ticks
    must flip from initial-None to HEALTHY on the second."""
    nats = AsyncMock()
    pub = FidelityVerdictPublisher(nats_client=nats, stable_ticks_required=2)

    p1 = await pub.maybe_publish(_result("ta", HEALTHY))
    assert p1 is False  # streak=1 — not yet
    p2 = await pub.maybe_publish(_result("ta", HEALTHY))
    assert p2 is True
    nats.publish.assert_awaited_once()
    subject, data = nats.publish.call_args.args
    assert subject == "evaluator.strategy.ta.verdict"
    body = json.loads(data.decode())
    assert body["verdict"] == HEALTHY


@pytest.mark.asyncio
async def test_publisher_does_not_republish_same_verdict():
    """Once HEALTHY has been emitted, subsequent HEALTHY ticks must
    NOT publish — only flips are interesting."""
    nats = AsyncMock()
    pub = FidelityVerdictPublisher(nats_client=nats, stable_ticks_required=1)

    await pub.maybe_publish(_result("ta", HEALTHY))  # emits
    nats.publish.reset_mock()
    await pub.maybe_publish(_result("ta", HEALTHY))  # no-op
    await pub.maybe_publish(_result("ta", HEALTHY))  # no-op
    nats.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publisher_flapping_does_not_emit_until_stable():
    """Alternating raw verdicts must not flip the emitted verdict
    because the candidate streak never reaches stable_ticks_required."""
    nats = AsyncMock()
    pub = FidelityVerdictPublisher(nats_client=nats, stable_ticks_required=3)

    await pub.maybe_publish(_result("ta", HEALTHY))  # streak=1
    await pub.maybe_publish(
        _result("ta", UNHEALTHY)
    )  # candidate reset to UNHEALTHY streak=1
    await pub.maybe_publish(_result("ta", HEALTHY))  # candidate reset streak=1
    await pub.maybe_publish(_result("ta", UNHEALTHY))  # streak=1
    nats.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publisher_publish_failure_does_not_advance_state():
    """If the NATS publish raises, the publisher must retry on the next
    matching-verdict tick rather than treating the flip as committed."""
    nats = AsyncMock()
    nats.publish = AsyncMock(side_effect=[RuntimeError("nats down"), None])
    pub = FidelityVerdictPublisher(nats_client=nats, stable_ticks_required=1)

    p1 = await pub.maybe_publish(_result("ta", UNHEALTHY))
    assert p1 is False  # publish raised
    p2 = await pub.maybe_publish(_result("ta", UNHEALTHY))
    assert p2 is True  # retried successfully
    assert nats.publish.await_count == 2


@pytest.mark.asyncio
async def test_publisher_truncates_reason_to_200_chars():
    nats = AsyncMock()
    pub = FidelityVerdictPublisher(nats_client=nats, stable_ticks_required=1)
    long_reason = "x" * 500
    await pub.maybe_publish(_result("ta", UNHEALTHY, reason=long_reason))
    args, _ = nats.publish.call_args
    _, data = args
    body = json.loads(data.decode())
    assert len(body["reason"]) <= 200


# ---------------------------------------------------------------------------
# FidelityScheduler.run_cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_publishes_per_strategy():
    """Each strategy's verdict goes through the publisher; the
    publisher's per-strategy hysteresis state is independent."""
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(
        return_value=[_pnl_event(-1.0) for _ in range(15)]
    )
    repo = _char_repo_stub(mean_return=1.0)
    nats = AsyncMock()

    scheduler = FidelityScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=nats,
        interval_seconds=300,
        stable_ticks_required=1,
        strategy_ids=["ta-momentum", "ta-reversion"],
        threshold=0.5,
    )

    # Two ticks so both strategies flip into emitted state (stable=1
    # means the first tick already flips; this exercises both
    # publisher entries).
    results1 = await scheduler.run_cycle()
    results2 = await scheduler.run_cycle()

    assert {r.strategy_id for r in results1} == {"ta-momentum", "ta-reversion"}
    assert {r.strategy_id for r in results2} == {"ta-momentum", "ta-reversion"}
    # Each strategy should have published exactly once (first cycle);
    # the second cycle is a same-verdict no-op.
    assert nats.publish.await_count == 2
    subjects = {c.args[0] for c in nats.publish.call_args_list}
    assert subjects == {
        "evaluator.strategy.ta-momentum.verdict",
        "evaluator.strategy.ta-reversion.verdict",
    }


@pytest.mark.asyncio
async def test_scheduler_survives_per_strategy_failure():
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(return_value=[_pnl_event(1.0) for _ in range(15)])
    repo = _char_repo_stub(mean_return=1.0)
    nats = AsyncMock()

    scheduler = FidelityScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=nats,
        interval_seconds=300,
        strategy_ids=["ok", "explodes"],
    )

    original = scheduler._service.evaluate

    async def flaky(strategy_id, *, start=None, end=None):
        if strategy_id == "explodes":
            raise RuntimeError("synthetic")
        return await original(strategy_id, start=start, end=end)

    scheduler._service.evaluate = flaky  # type: ignore[assignment]
    results = await scheduler.run_cycle()
    assert [r.strategy_id for r in results] == ["ok"]


@pytest.mark.asyncio
async def test_scheduler_falls_back_to_mongo_discovery():
    adapter = MagicMock()
    adapter._connected = True
    adapter.find_filtered = AsyncMock(return_value=[])
    adapter.list_all_strategy_ids = AsyncMock(return_value=["s1", "s2"])
    repo = _char_repo_stub(mean_return=None)
    nats = AsyncMock()

    scheduler = FidelityScheduler(
        mongodb_adapter=adapter,
        characterization_repository=repo,
        nats_client=nats,
        strategy_ids=None,
    )
    results = await scheduler.run_cycle()
    assert {r.strategy_id for r in results} == {"s1", "s2"}
    adapter.list_all_strategy_ids.assert_awaited_once()


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
    r = client.get("/api/v1/strategy/ta-momentum/fidelity")
    assert r.status_code == 503


def test_route_returns_fidelity_result():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    fake = _result("ta-momentum", HEALTHY)
    with patch(
        "data_manager.strategies.fidelity_service.FidelityService.evaluate",
        new=AsyncMock(return_value=fake),
    ):
        client = _client_with_db(db)
        r = client.get("/api/v1/strategy/ta-momentum/fidelity")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "ta-momentum"
    assert body["verdict"] == HEALTHY


def test_route_500_when_service_raises():
    db = MagicMock()
    db.mongodb_adapter = MagicMock()
    db.mysql_adapter = MagicMock()
    with patch(
        "data_manager.strategies.fidelity_service.FidelityService.evaluate",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        client = _client_with_db(db)
        r = client.get("/api/v1/strategy/ta-momentum/fidelity")
    assert r.status_code == 500
