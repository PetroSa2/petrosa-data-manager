"""Tests for the P2.5 AuditEvaluator (#596).

Validates the three cross-service detectors (consume-without-persist,
decision_id propagation, join completeness) and the boundary cases.
Counters and events are injected directly so tests are deterministic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from data_manager.auditor.audit_evaluator import (
    DEFAULT_LOOKBACK_S,
    MIN_CONSUME_RATE_RECEIPTS,
    SUBSYSTEM,
    AuditEvaluator,
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


def _empty_events() -> Callable[
    [str, datetime, datetime], Awaitable[list[dict[str, object]]]
]:
    async def _src(name: str, start: datetime, end: datetime):
        return []

    return _src


def _events_by_collection(
    by_name: dict[str, list[dict[str, object]]],
) -> Callable[[str, datetime, datetime], Awaitable[list[dict[str, object]]]]:
    async def _src(name: str, start: datetime, end: datetime):
        return [e for e in by_name.get(name, []) if start <= e["timestamp"] < end]

    return _src


def _counters(snapshots: list[dict[str, tuple[int, int]]]):
    """Return a counter_source closure that hands out snapshots in order."""
    idx = [0]

    def _source() -> dict[str, tuple[int, int]]:
        i = min(idx[0], len(snapshots) - 1)
        idx[0] += 1
        return snapshots[i]

    return _source


# ----------------------------------------------------------------------
# Subsystem + boundary tests.
# ----------------------------------------------------------------------


def test_subsystem_constant_matches_contract():
    assert SUBSYSTEM == "audit"


@pytest.mark.asyncio
async def test_no_traffic_no_counter_history_returns_unknown():
    clock = _Clock(T0)
    ev = AuditEvaluator(
        counter_source=lambda: {},
        event_source=_empty_events(),
        time_source=clock,
    )
    verdict, reason = await ev.evaluate()
    assert verdict == "unknown"
    assert "no traffic" in reason


@pytest.mark.asyncio
async def test_healthy_when_consume_equals_persist_and_no_orphans():
    clock = _Clock(T0)
    counters = _counters(
        [
            {"intent": (100, 100), "decision": (50, 50), "execution": (200, 200)},
            {"intent": (200, 200), "decision": (100, 100), "execution": (400, 400)},
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_empty_events(),
        time_source=clock,
    )
    # First tick establishes baseline; second tick should be healthy.
    await ev.evaluate()
    verdict, reason = await ev.evaluate()
    assert verdict == "healthy"
    assert "consume=persist" in reason


# ----------------------------------------------------------------------
# Consume-without-persist detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absolute_budget_trips_on_growing_gap():
    """A single stream losing more than the absolute tick budget trips."""
    clock = _Clock(T0)
    counters = _counters(
        [
            {"intent": (100, 100)},
            # +50 received, +30 persisted → 20 unpersisted in this tick.
            {"intent": (150, 130)},
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_empty_events(),
        time_source=clock,
        consume_persist_abs_budget=5,
    )
    await ev.evaluate()  # baseline
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "intent" in reason
    assert "unpersisted" in reason


@pytest.mark.asyncio
async def test_relative_budget_trips_on_high_volume_loss():
    """At high volume even a small absolute gap can exceed the relative budget."""
    clock = _Clock(T0)
    big = MIN_CONSUME_RATE_RECEIPTS + 200
    counters = _counters(
        [
            {"execution": (0, 0)},
            # 300 received, 296 persisted = 4 unpersisted (below abs budget 5)
            # but 4/300 = 1.33% > 0.5% relative budget.
            {"execution": (big, big - 4)},
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_empty_events(),
        time_source=clock,
        consume_persist_abs_budget=10,
        consume_persist_rel_budget=0.005,
    )
    await ev.evaluate()
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "execution" in reason
    assert "%" in reason


@pytest.mark.asyncio
async def test_relative_budget_silent_at_low_volume():
    """Below MIN_CONSUME_RATE_RECEIPTS, ratio detector stays silent."""
    clock = _Clock(T0)
    counters = _counters(
        [
            {"intent": (0, 0)},
            # Only a handful of receipts; ratio detector should stay silent.
            {"intent": (10, 9)},
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_empty_events(),
        time_source=clock,
        consume_persist_abs_budget=5,
        consume_persist_rel_budget=0.005,
    )
    await ev.evaluate()
    verdict, _ = await ev.evaluate()
    # Below the absolute budget AND below the volume floor for ratio →
    # neither trips. Healthy.
    assert verdict == "healthy"


@pytest.mark.asyncio
async def test_counter_reset_does_not_trip():
    """Pod restart resets counters to 0 — must not look like loss."""
    clock = _Clock(T0)
    counters = _counters(
        [
            {"intent": (500, 500)},
            # Counter reset (pod restart) — both back to zero.
            {"intent": (0, 0)},
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_empty_events(),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, _ = await ev.evaluate()
    # Negative delta should be ignored; no signal → healthy.
    assert verdict == "healthy"


@pytest.mark.asyncio
async def test_first_tick_establishes_baseline_only():
    """First call should not trip even if absolute counters are huge."""
    clock = _Clock(T0)
    counters = _counters(
        [
            {"intent": (10_000, 0)},  # Looks catastrophic but it's the baseline.
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_empty_events(),
        time_source=clock,
    )
    verdict, _ = await ev.evaluate()
    # Baseline tick — no prior delta, no signal. "unknown" (no traffic +
    # no counter history yet) is acceptable; "unhealthy" is not.
    assert verdict != "unhealthy"


# ----------------------------------------------------------------------
# decision_id propagation detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_decision_id_on_order_trips():
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=60),
                "event_type": "placed",
                "order_id": "O1",
                "decision_id": "",  # MISSING — contract violation.
                "strategy_id": "S1",
            },
        ],
    }
    counters = _counters([{}, {}])
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()  # baseline
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "missing decision_id" in reason
    assert "O1" in reason


@pytest.mark.asyncio
async def test_missing_decision_id_on_fill_trips():
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=30),
                "event_type": "filled",
                "order_id": "O7",
                "decision_id": None,
                "strategy_id": "S1",
            },
        ],
    }
    counters = _counters([{}, {}])
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "missing decision_id" in reason
    assert "O7" in reason


@pytest.mark.asyncio
async def test_decision_id_required_only_for_orders_and_fills():
    """Hypothetical event types without decision_id must not trip."""
    clock = _Clock(T0)
    events = {
        "execution_events": [
            # Not a known required event type — should NOT trip
            # decision_id propagation.
            {
                "timestamp": T0 - timedelta(seconds=30),
                "event_type": "ack",
                "order_id": "O1",
                "decision_id": None,
                "strategy_id": "S1",
            },
        ],
    }
    ev = AuditEvaluator(
        counter_source=_counters([{}, {}]),
        event_source=_events_by_collection(events),
        time_source=_Clock(T0),
    )
    await ev.evaluate()
    verdict, _ = await ev.evaluate()
    assert verdict == "healthy"


# ----------------------------------------------------------------------
# Join-completeness detector.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_order_without_parent_decision_trips():
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=60),
                "event_type": "placed",
                "order_id": "OX",
                "decision_id": "D-MISSING",
                "strategy_id": "S1",
            },
        ],
        "cio_decisions": [
            # Only an unrelated decision — D-MISSING isn't here.
            {
                "timestamp": T0 - timedelta(seconds=120),
                "decision_id": "D-OTHER",
                "_id": "D-OTHER",
                "strategy_id": "S1",
            },
        ],
    }
    ev = AuditEvaluator(
        counter_source=_counters([{}, {}]),
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "orphan" in reason
    assert "OX" in reason


@pytest.mark.asyncio
async def test_orphan_fill_without_sibling_order_trips():
    clock = _Clock(T0)
    events = {
        "execution_events": [
            # Fill alone — no `placed` row in the window, no parent
            # decision either.
            {
                "timestamp": T0 - timedelta(seconds=30),
                "event_type": "filled",
                "order_id": "OY",
                "decision_id": "D-MISSING",
                "strategy_id": "S1",
            },
        ],
        "cio_decisions": [],
    }
    ev = AuditEvaluator(
        counter_source=_counters([{}, {}]),
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "orphan" in reason


@pytest.mark.asyncio
async def test_fill_with_sibling_placed_in_window_is_healthy():
    """If the `placed` row for the same order_id is in-window the fill joins."""
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=120),
                "event_type": "placed",
                "order_id": "OZ",
                "decision_id": "DZ",
                "strategy_id": "S1",
            },
            {
                "timestamp": T0 - timedelta(seconds=60),
                "event_type": "filled",
                "order_id": "OZ",
                "decision_id": "DZ",
                "strategy_id": "S1",
            },
        ],
        "cio_decisions": [
            {
                "timestamp": T0 - timedelta(seconds=180),
                "decision_id": "DZ",
                "_id": "DZ",
                "strategy_id": "S1",
            },
        ],
    }
    ev = AuditEvaluator(
        counter_source=_counters([{}, {}]),
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, reason = await ev.evaluate()
    assert verdict == "healthy"
    assert "joins complete" in reason


@pytest.mark.asyncio
async def test_join_match_via_underscore_id_field():
    """Decision_consumer stores decision_id at `_id` too — joiner must check both."""
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=30),
                "event_type": "placed",
                "order_id": "O1",
                "decision_id": "D-FROM-ID-FIELD",
                "strategy_id": "S1",
            },
        ],
        "cio_decisions": [
            # Some pipelines persist with only `_id`, not `decision_id`.
            {
                "timestamp": T0 - timedelta(seconds=120),
                "_id": "D-FROM-ID-FIELD",
                "strategy_id": "S1",
            },
        ],
    }
    ev = AuditEvaluator(
        counter_source=_counters([{}, {}]),
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, _ = await ev.evaluate()
    assert verdict == "healthy"


# ----------------------------------------------------------------------
# Detector priority — consume-without-persist > propagation > join.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_persist_wins_over_orphan_signal():
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=30),
                "event_type": "placed",
                "order_id": "O1",
                "decision_id": "D-MISSING",
                "strategy_id": "S1",
            },
        ],
        "cio_decisions": [],
    }
    counters = _counters(
        [
            {"execution": (0, 0)},
            {"execution": (50, 30)},  # 20 unpersisted — well past abs budget.
        ]
    )
    ev = AuditEvaluator(
        counter_source=counters,
        event_source=_events_by_collection(events),
        time_source=clock,
        consume_persist_abs_budget=5,
    )
    await ev.evaluate()  # baseline
    verdict, reason = await ev.evaluate()
    assert verdict == "unhealthy"
    assert "unpersisted" in reason  # the consume detector won.


# ----------------------------------------------------------------------
# Lookback window honored.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_events_outside_lookback_are_ignored():
    """An orphan order older than the lookback must not trip."""
    clock = _Clock(T0)
    events = {
        "execution_events": [
            {
                "timestamp": T0 - timedelta(seconds=DEFAULT_LOOKBACK_S + 60),
                "event_type": "placed",
                "order_id": "ANCIENT",
                "decision_id": "D-MISSING",
                "strategy_id": "S1",
            },
        ],
        "cio_decisions": [],
    }
    ev = AuditEvaluator(
        counter_source=_counters([{}, {}]),
        event_source=_events_by_collection(events),
        time_source=clock,
    )
    await ev.evaluate()
    verdict, _ = await ev.evaluate()
    assert verdict == "healthy"
