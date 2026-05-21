"""Tests for the P2.1 GapDetectorEvaluator consumer (#634).

Validates:
  * Verdict-shape (frozen ``EvaluatorVerdict`` schema, payload keys).
  * Cycle-sample classification (healthy ↔ unhealthy thresholds).
  * Hysteresis behavior — single-cycle blips do not flip the published
    verdict (NFR-R1 "detection-time" smoothing).
  * Publisher wiring — `tick_with_sample` calls the framework publisher
    with the right subject + JSON payload.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from petrosa_otel.evaluators import (
    ConsecutiveSamplesHysteresis,
    NatsVerdictPublisher,
)

from data_manager.auditor.evaluator import (
    SUBSYSTEM,
    GapDetectorEvaluator,
)


def test_cycle_sample_healthy_when_no_gaps():
    verdict, reason = GapDetectorEvaluator.cycle_sample(total_gaps=0)
    assert verdict == "healthy"
    assert reason == "no gaps in audit cycle"


def test_cycle_sample_unhealthy_with_worst_summary():
    verdict, reason = GapDetectorEvaluator.cycle_sample(
        total_gaps=3,
        worst_gap_summary="BTCUSDT 1m, 7200s",
    )
    assert verdict == "unhealthy"
    assert "3" in reason
    assert "BTCUSDT 1m, 7200s" in reason
    # NFR-O5: single-line, ≤200 chars.
    assert "\n" not in reason
    assert len(reason) <= 200


def test_cycle_sample_unhealthy_without_summary():
    verdict, reason = GapDetectorEvaluator.cycle_sample(total_gaps=1)
    assert verdict == "unhealthy"
    assert "1" in reason


@pytest.mark.asyncio
async def test_tick_publishes_to_correct_subject():
    nats_client = AsyncMock()
    nats_client.publish = AsyncMock()
    publisher = NatsVerdictPublisher(nats_client=nats_client)
    # n=1 so the verdict commits on the first sample — keeps the test
    # focused on publish shape, not hysteresis.
    evaluator = GapDetectorEvaluator(
        publisher=publisher,
        hysteresis=ConsecutiveSamplesHysteresis(n=1),
    )

    sample_verdict, sample_reason = GapDetectorEvaluator.cycle_sample(
        total_gaps=2, worst_gap_summary="ETHUSDT 15m, 1800s"
    )
    verdict_obj = await evaluator.tick_with_sample(sample_verdict, sample_reason)

    assert verdict_obj.subsystem == SUBSYSTEM
    assert verdict_obj.verdict == "unhealthy"

    nats_client.publish.assert_awaited_once()
    subject, body = nats_client.publish.call_args.args
    assert subject == f"evaluator.{SUBSYSTEM}.verdict"
    payload = json.loads(body.decode())
    assert payload["subsystem"] == SUBSYSTEM
    assert payload["verdict"] == "unhealthy"
    assert "ETHUSDT 15m, 1800s" in payload["reason"]
    # Hysteresis state is included so consumers can audit the smoothing.
    assert payload["hysteresis"]["policy"] == "consecutive_samples"


@pytest.mark.asyncio
async def test_hysteresis_smooths_single_cycle_blip():
    """A single unhealthy cycle surrounded by healthy ones must not flip
    the published verdict — the framework's default hysteresis (n=3)
    delivers NFR-R1's "detection-time" smoothing for free."""
    nats_client = AsyncMock()
    nats_client.publish = AsyncMock()
    publisher = NatsVerdictPublisher(nats_client=nats_client)
    evaluator = GapDetectorEvaluator(
        publisher=publisher,
        hysteresis=ConsecutiveSamplesHysteresis(n=3),
    )

    # Three healthy ticks → committed healthy.
    for _ in range(3):
        sv, sr = GapDetectorEvaluator.cycle_sample(total_gaps=0)
        await evaluator.tick_with_sample(sv, sr)

    # Single unhealthy blip — must NOT flip the committed verdict.
    sv, sr = GapDetectorEvaluator.cycle_sample(
        total_gaps=1, worst_gap_summary="ADAUSDT 1h, 3600s"
    )
    verdict_obj = await evaluator.tick_with_sample(sv, sr)
    assert verdict_obj.verdict == "healthy"

    # Two more unhealthy ticks → committed unhealthy on the third.
    for _ in range(2):
        sv, sr = GapDetectorEvaluator.cycle_sample(
            total_gaps=2, worst_gap_summary="ADAUSDT 1h, 3600s"
        )
        verdict_obj = await evaluator.tick_with_sample(sv, sr)
    assert verdict_obj.verdict == "unhealthy"


@pytest.mark.asyncio
async def test_evaluator_works_without_publisher():
    """Evaluator must be constructable without a NATS client — local
    dev / unit tests must not require a broker."""
    evaluator = GapDetectorEvaluator(
        publisher=None,
        hysteresis=ConsecutiveSamplesHysteresis(n=1),
    )
    sv, sr = GapDetectorEvaluator.cycle_sample(total_gaps=0)
    verdict_obj = await evaluator.tick_with_sample(sv, sr)
    assert verdict_obj.verdict == "healthy"
    assert verdict_obj.subsystem == SUBSYSTEM
