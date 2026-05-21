"""Tests for the P2.2 IngestEvaluator (#593).

Validates the three detectors (silence, staleness, integrity) and the
no-message-yet path. Time is controlled via a faux time_source so the
tests are deterministic and fast.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data_manager.auditor.ingest_evaluator import (
    DEFAULT_SILENCE_THRESHOLD_S,
    DEFAULT_STALENESS_THRESHOLD_S,
    SUBSYSTEM,
    IngestEvaluator,
)

SUBJECT = "binance.futures.websocket.data"


class _Clock:
    """Pinnable clock for deterministic tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _make(clock: _Clock, **overrides) -> IngestEvaluator:
    return IngestEvaluator(
        subjects=[SUBJECT],
        time_source=clock,
        **overrides,
    )


def test_initial_state_is_unknown_with_no_messages():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    verdict, reason = ev.current_sample()
    assert verdict == "unknown"
    assert "no message yet" in reason


def test_healthy_after_recent_message_with_fresh_payload():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    ev.record_message(SUBJECT, payload_timestamp=clock())
    verdict, reason = ev.current_sample()
    assert verdict == "healthy"
    assert reason == "no silence, staleness, or integrity issues"


def test_silence_unhealthy_past_threshold():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    ev.record_message(SUBJECT, payload_timestamp=clock())
    clock.advance(DEFAULT_SILENCE_THRESHOLD_S + 5)
    verdict, reason = ev.current_sample()
    assert verdict == "unhealthy"
    assert "silent" in reason
    assert SUBJECT in reason


def test_silence_below_threshold_still_healthy():
    """Within the silence budget, no other detector should fire either."""
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    # Use a generous staleness threshold so the test isolates silence —
    # otherwise advancing the clock past 30 s would trip staleness on
    # the original payload_ts.
    ev = _make(clock, staleness_threshold_s=120)
    ev.record_message(SUBJECT, payload_timestamp=clock())
    clock.advance(DEFAULT_SILENCE_THRESHOLD_S - 1)
    verdict, _ = ev.current_sample()
    assert verdict == "healthy"


def test_staleness_unhealthy_when_payload_lags_behind():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    stale_payload = clock() - timedelta(seconds=DEFAULT_STALENESS_THRESHOLD_S + 5)
    ev.record_message(SUBJECT, payload_timestamp=stale_payload)
    verdict, reason = ev.current_sample()
    assert verdict == "unhealthy"
    assert "stale" in reason


def test_staleness_silently_ignored_without_payload_ts():
    """Consumers without a timestamp field shouldn't trip staleness."""
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    ev.record_message(SUBJECT, payload_timestamp=None)
    verdict, _ = ev.current_sample()
    assert verdict == "healthy"


def test_integrity_loss_unhealthy_past_budget():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(
        clock,
        integrity_failure_budget=2,
        integrity_window_s=60,
    )
    ev.record_message(SUBJECT, payload_timestamp=clock())
    for _ in range(3):  # exceeds budget of 2
        ev.record_parse_failure(SUBJECT)
        clock.advance(1)
    verdict, reason = ev.current_sample()
    assert verdict == "unhealthy"
    assert "parse failures" in reason


def test_integrity_failures_age_out_of_window():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(
        clock,
        integrity_failure_budget=2,
        integrity_window_s=30,
    )
    ev.record_message(SUBJECT, payload_timestamp=clock())
    # Three failures inside the window — would trip.
    for _ in range(3):
        ev.record_parse_failure(SUBJECT)
    assert ev.current_sample()[0] == "unhealthy"
    # Advance past the integrity window AND record fresh message — old
    # failures should have aged out, new message keeps the silence/
    # staleness detectors happy.
    clock.advance(35)
    ev.record_message(SUBJECT, payload_timestamp=clock())
    assert ev.current_sample()[0] == "healthy"


def test_silence_wins_over_staleness_in_reason():
    """When both fire, silence is the more actionable signal."""
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    stale_payload = clock() - timedelta(seconds=DEFAULT_STALENESS_THRESHOLD_S + 5)
    ev.record_message(SUBJECT, payload_timestamp=stale_payload)
    clock.advance(DEFAULT_SILENCE_THRESHOLD_S + 5)
    verdict, reason = ev.current_sample()
    assert verdict == "unhealthy"
    assert "silent" in reason


def test_untracked_subject_is_silently_ignored():
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    # Should not raise; state for SUBJECT must stay untouched.
    ev.record_message("other.subject", payload_timestamp=clock())
    ev.record_parse_failure("other.subject")
    assert ev.current_sample()[0] == "unknown"


@pytest.mark.asyncio
async def test_evaluator_subsystem_and_subject_template():
    """tick() should publish on `evaluator.ingest.verdict` per framework."""
    clock = _Clock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    ev = _make(clock)
    ev.record_message(SUBJECT, payload_timestamp=clock())
    verdict_obj = await ev.tick()
    assert verdict_obj.subsystem == SUBSYSTEM
    # Smoke: tick() goes through the framework's commit + publish path
    # even when no publisher is wired — that's the no-broker scenario.
    assert verdict_obj.verdict in ("healthy", "unhealthy", "unknown")
