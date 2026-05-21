"""Tests for the P2.4 ExecutionEvaluator (#595).

Validates the four detectors (exchange error rate, fill rate, risk-posture
drift, slippage) and the boundary cases. Events are injected directly via
an in-memory ``event_provider`` and time is pinned via ``time_source`` so
tests are deterministic and fast.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from data_manager.auditor.execution_evaluator import (
    DEFAULT_ERROR_WINDOW_S,
    DEFAULT_FILL_RATE_WINDOW_S,
    DEFAULT_RISK_BASELINE_S,
    DEFAULT_RISK_WINDOW_S,
    DEFAULT_SLIPPAGE_WINDOW_S,
    MIN_ERROR_RATE_TOTAL,
    MIN_FILL_RATE_PLACED_SAMPLES,
    MIN_RISK_BASELINE_SAMPLES,
    MIN_SLIPPAGE_SAMPLES,
    SUBSYSTEM,
    ExecutionEvaluator,
)

T0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


class _Clock:
    """Pinnable clock for deterministic tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _make_event(
    *,
    event_type: str,
    ts: datetime,
    strategy_id: str = "S1",
    symbol: str = "BTCUSDT",
    qty: float | None = 1.0,
    fill_qty: float | None = None,
    price: float | None = 100.0,
    reason: str | None = None,
    decision_id: str = "D1",
    order_id: str = "O1",
) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "strategy_id": strategy_id,
        "order_id": order_id,
        "event_type": event_type,
        "timestamp": ts,
        "symbol": symbol,
        "qty": qty,
        "fill_qty": fill_qty,
        "price": price,
        "reason": reason,
    }


def _provider(
    events: list[dict[str, object]],
) -> Callable[[datetime, datetime], Awaitable[list[dict[str, object]]]]:
    async def _p(start: datetime, end: datetime) -> list[dict[str, object]]:
        return [e for e in events if start <= e["timestamp"] < end]

    return _p


def _make(
    clock: _Clock,
    events: list[dict[str, object]],
    **overrides,
) -> ExecutionEvaluator:
    return ExecutionEvaluator(
        event_provider=_provider(events),
        time_source=clock,
        **overrides,
    )


# ----------------------------------------------------------------------
# Subsystem / boundary tests.
# ----------------------------------------------------------------------


def test_subsystem_constant_matches_contract():
    assert SUBSYSTEM == "execution"


@pytest.mark.asyncio
async def test_no_events_returns_unknown():
    clock = _Clock(T0)
    ev = _make(clock, [])
    verdict, reason = await ev.evaluate()
    assert verdict == "unknown"
    assert "no execution events" in reason


@pytest.mark.asyncio
async def test_healthy_when_all_detectors_silent():
    clock = _Clock(T0)
    # 20 fills spread over the slippage window at constant price.
    events = [
        _make_event(
            event_type="filled",
            ts=T0 - timedelta(seconds=60 * i),
            price=100.0,
            fill_qty=1.0,
        )
        for i in range(20)
    ]
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "healthy"
    assert "no slippage" in reason


# ----------------------------------------------------------------------
# Exchange-error detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_rate_trips_at_budget():
    clock = _Clock(T0)
    events = []
    # 80 placed + 20 rejected = ~20% rejection rate (budget 5%).
    # Shift timestamps by 1s so none land exactly on T0 (provider uses
    # half-open `[start, end)` so T0 itself is excluded).
    for i in range(80):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=i + 1),
                order_id=f"O{i}",
            )
        )
    for i in range(20):
        events.append(
            _make_event(
                event_type="rejected",
                ts=T0 - timedelta(seconds=i + 90),
                order_id=f"R{i}",
                reason="balance_below_minimum",
            )
        )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "rejected" in reason
    assert "20.00% of 100 events" in reason


@pytest.mark.asyncio
async def test_rate_limit_rejections_trip_at_tighter_budget():
    """rate-limit rejections fire at 1% even when overall rejects are healthy.

    Build a fill stream so the fill-rate detector stays silent and the
    error-rate detector is the only one in play; then mix in 2 of 200
    rate-limit rejects (=1% — just over the 1% rate-limit budget).
    """
    clock = _Clock(T0)
    events = []
    # 198 placed + 198 filled = healthy fill rate, well above MIN_FILL_RATE.
    for i in range(198):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=i + 1),
                order_id=f"O{i}",
            )
        )
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=i + 2),
                order_id=f"O{i}",
                fill_qty=1.0,
            )
        )
    # 2 of 200+ events in the 5-min window are 429 rate-limit rejections.
    # We need rate-limit rate > 1%; the error detector window only sees
    # events within the last 300s, so all events above are inside it.
    # 2 rate-limit / (198 placed + 198 filled + 2 rejected) within 300s
    # = 2/398 ≈ 0.5% — too low. Push the fill events past the error
    # window so the error-detector denominator drops.
    events = [e for e in events if e["event_type"] != "filled"]
    for i in range(198):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=DEFAULT_ERROR_WINDOW_S + 60 + i),
                order_id=f"O{i}",
                fill_qty=1.0,
            )
        )
    # Now in the 300s error window: 198 placed + 0 filled + (we add) 3
    # rate-limit rejects = 201 events. 3/201 ≈ 1.49% > 1% budget.
    for i in range(3):
        events.append(
            _make_event(
                event_type="rejected",
                ts=T0 - timedelta(seconds=i + 100),
                order_id=f"R{i}",
                reason=f"429 too many requests #{i}",
            )
        )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "rate-limit" in reason


@pytest.mark.asyncio
async def test_error_detector_silent_below_sample_floor():
    """Below the min-sample floor the detector should NOT trip."""
    clock = _Clock(T0)
    # 1 placed + 1 rejected. Total well under MIN_ERROR_RATE_TOTAL.
    events = [
        _make_event(event_type="placed", ts=T0 - timedelta(seconds=10)),
        _make_event(
            event_type="rejected",
            ts=T0 - timedelta(seconds=15),
            reason="429",
        ),
    ]
    ev = _make(clock, events)
    verdict, _ = await ev.evaluate()
    # With only 2 events in the error window and no other detector data,
    # we fall through to "healthy" — there is data in the lookback so
    # we don't return "unknown".
    assert verdict == "healthy"


@pytest.mark.asyncio
async def test_old_rejections_outside_error_window_do_not_trip():
    """Rejections older than the error-rate window must not contribute."""
    clock = _Clock(T0)
    events = []
    # 30 ancient rejects (just inside the broader lookback so the
    # provider returns them, but past the 5-min error window).
    for i in range(30):
        events.append(
            _make_event(
                event_type="rejected",
                ts=T0 - timedelta(seconds=DEFAULT_ERROR_WINDOW_S + 60 + i),
                order_id=f"R{i}",
                reason="exchange_error",
            )
        )
    # 5 recent placed orders within error window.
    for i in range(5):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=10 + i),
                order_id=f"O{i}",
            )
        )
    ev = _make(clock, events)
    verdict, _ = await ev.evaluate()
    assert verdict == "healthy"


# ----------------------------------------------------------------------
# Fill-rate detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_rate_trips_when_below_minimum():
    clock = _Clock(T0)
    # 20 placed, only 5 filled = 25% fill rate, below default 50%.
    events = []
    for i in range(20):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=60 + i),
                order_id=f"O{i}",
            )
        )
    for i in range(5):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=120 + i),
                order_id=f"O{i}",
                fill_qty=1.0,
            )
        )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "fill rate" in reason
    assert "25%" in reason or "25.00%" in reason


@pytest.mark.asyncio
async def test_fill_rate_silent_with_few_placed():
    """Should not trip until MIN_FILL_RATE_PLACED_SAMPLES placed orders accrue."""
    clock = _Clock(T0)
    events = []
    # Only 3 placed orders, 0 filled — well below the sample floor.
    for i in range(3):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=60 + i),
                order_id=f"O{i}",
            )
        )
    ev = _make(clock, events)
    verdict, _ = await ev.evaluate()
    assert verdict == "healthy"


@pytest.mark.asyncio
async def test_fill_rate_isolates_to_strategy_symbol_pair():
    """Healthy strategy/symbol shouldn't mask an unhealthy peer."""
    clock = _Clock(T0)
    events = []
    # S1/BTCUSDT: 20 placed, 20 filled — healthy.
    for i in range(20):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=60 + i),
                strategy_id="S1",
                symbol="BTCUSDT",
                order_id=f"A{i}",
            )
        )
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=120 + i),
                strategy_id="S1",
                symbol="BTCUSDT",
                order_id=f"A{i}",
                fill_qty=1.0,
            )
        )
    # S2/ETHUSDT: 15 placed, 2 filled = ~13% — should trip.
    for i in range(15):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=60 + i),
                strategy_id="S2",
                symbol="ETHUSDT",
                order_id=f"B{i}",
            )
        )
    for i in range(2):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=120 + i),
                strategy_id="S2",
                symbol="ETHUSDT",
                order_id=f"B{i}",
                fill_qty=1.0,
            )
        )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "S2/ETHUSDT" in reason


# ----------------------------------------------------------------------
# Risk-posture detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_drift_trips_on_velocity_spike():
    clock = _Clock(T0)
    events = []
    # Baseline: small steady flow across many baseline buckets.
    num_baseline_buckets = DEFAULT_RISK_BASELINE_S // DEFAULT_RISK_WINDOW_S
    for bucket in range(1, num_baseline_buckets + 1):
        # Each baseline bucket has a single $100 notional fill.
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=bucket * DEFAULT_RISK_WINDOW_S + 60),
                fill_qty=1.0,
                price=100.0,
            )
        )
    # Recent: massive spike — $1000 in recent window (10x median).
    for i in range(10):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=60 + i),
                fill_qty=1.0,
                price=100.0,
                order_id=f"F{i}",
            )
        )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "risk velocity" in reason


@pytest.mark.asyncio
async def test_risk_drift_silent_without_baseline_samples():
    """Should not trip when baseline has too few non-zero buckets."""
    clock = _Clock(T0)
    events = []
    # Only a few baseline samples (below MIN_RISK_BASELINE_SAMPLES).
    for bucket in range(1, MIN_RISK_BASELINE_SAMPLES - 1):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=bucket * DEFAULT_RISK_WINDOW_S + 60),
                fill_qty=1.0,
                price=100.0,
            )
        )
    # Recent spike.
    for i in range(20):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=60 + i),
                fill_qty=1.0,
                price=100.0,
                order_id=f"F{i}",
            )
        )
    ev = _make(clock, events)
    verdict, _ = await ev.evaluate()
    # Risk detector should be silent; slippage may or may not be —
    # accept any non-unhealthy-risk verdict.
    if verdict == "unhealthy":
        assert "risk velocity" not in (await ev.evaluate())[1]


# ----------------------------------------------------------------------
# Slippage detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slippage_trips_on_outlier_price():
    clock = _Clock(T0)
    events = []
    # 40 fills at $100 with tiny noise.
    for i in range(40):
        price = 100.0 + (0.01 if i % 2 else -0.01)
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=60 + i),
                price=price,
                fill_qty=1.0,
                order_id=f"F{i}",
            )
        )
    # Latest fill (most recent timestamp) is a massive outlier.
    events.append(
        _make_event(
            event_type="filled",
            ts=T0 - timedelta(seconds=1),
            price=200.0,
            fill_qty=1.0,
            order_id="F-spike",
        )
    )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    # Could trip via slippage OR risk velocity (the outlier doubles notional).
    # We accept either as long as the verdict is unhealthy.
    assert verdict == "unhealthy"
    assert "z=" in reason or "risk velocity" in reason


@pytest.mark.asyncio
async def test_slippage_silent_below_min_samples():
    """Slippage detector must wait for MIN_SLIPPAGE_SAMPLES fills."""
    clock = _Clock(T0)
    events = []
    # Only 5 fills (well below MIN_SLIPPAGE_SAMPLES of 30).
    for i in range(5):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=60 + i),
                price=100.0 if i < 4 else 1000.0,
                fill_qty=1.0,
                order_id=f"F{i}",
            )
        )
    ev = _make(clock, events)
    verdict, _ = await ev.evaluate()
    # Slippage detector silent; not enough other data to trip anything either.
    assert verdict in {"healthy", "unknown"}


# ----------------------------------------------------------------------
# Detector priority — error-rate > fill-rate > risk > slippage.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_rate_wins_over_fill_rate_when_both_trip():
    clock = _Clock(T0)
    events = []
    # Fill rate would trip: 20 placed, 5 filled in 1h.
    for i in range(20):
        events.append(
            _make_event(
                event_type="placed",
                ts=T0 - timedelta(seconds=60 + i),
                order_id=f"P{i}",
            )
        )
    for i in range(5):
        events.append(
            _make_event(
                event_type="filled",
                ts=T0 - timedelta(seconds=120 + i),
                order_id=f"P{i}",
                fill_qty=1.0,
            )
        )
    # Error rate also trips: 20 rejected in 5m window.
    for i in range(20):
        events.append(
            _make_event(
                event_type="rejected",
                ts=T0 - timedelta(seconds=10 + i),
                order_id=f"R{i}",
                reason="balance_below_minimum",
            )
        )
    ev = _make(clock, events)
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    # Either rejected or rate-limit reason — both come from the error detector.
    assert "rejected" in reason or "rate-limit" in reason


# ----------------------------------------------------------------------
# Provider edge-cases.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_exception_returns_unknown():
    clock = _Clock(T0)

    async def bad_provider(start: datetime, end: datetime) -> list[dict[str, object]]:
        raise RuntimeError("mongo down")

    ev = ExecutionEvaluator(event_provider=bad_provider, time_source=clock)
    verdict, reason = await ev.evaluate()
    assert verdict == "unknown"
    assert "event provider error" in reason


@pytest.mark.asyncio
async def test_unparseable_timestamps_are_skipped():
    """Rows with malformed timestamps shouldn't crash the detector."""
    clock = _Clock(T0)
    events = [
        {
            "decision_id": "D",
            "strategy_id": "S",
            "order_id": "O",
            "event_type": "filled",
            "timestamp": "not-a-date",
            "symbol": "BTCUSDT",
            "qty": 1.0,
            "fill_qty": 1.0,
            "price": 100.0,
        },
    ]

    async def passthrough(start: datetime, end: datetime) -> list[dict[str, object]]:
        return events

    ev = ExecutionEvaluator(event_provider=passthrough, time_source=clock)
    verdict, _ = await ev.evaluate()
    # The single malformed row falls outside every detector window
    # (its timestamp normalises to epoch). Provider returned 1 row so
    # we don't return "unknown" — fall through to healthy.
    assert verdict == "healthy"
