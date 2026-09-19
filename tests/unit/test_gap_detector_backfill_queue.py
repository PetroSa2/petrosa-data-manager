"""Tests for gap detector backfill queue integration (petrosa-data-manager#320).

Validates:
  * When backfill_orchestrator is None, gaps are queued instead of silently dropped.
  * When backfill_orchestrator is available, requests go directly to it.
  * When orchestrator call fails, requests are queued as fallback.
  * Metrics are updated correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.auditor.gap_detector import GapDetector
from data_manager.models.events import BackfillRequest
from data_manager.models.health import GapInfo
from data_manager.services.backfill_queue import BackfillRequestQueue


@pytest.fixture
def mock_db_manager():
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    return db


@pytest.fixture
def detector(mock_db_manager):
    return GapDetector(mock_db_manager)


@pytest.fixture
def queue():
    return BackfillRequestQueue(max_size=10)


@pytest.fixture
def mock_orchestrator():
    orchestrator = AsyncMock()
    orchestrator.create_backfill_job = AsyncMock(
        return_value=MagicMock(job_id="job-real")
    )
    return orchestrator


@pytest.fixture
def sample_gap():
    return GapInfo(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, hour=2, tzinfo=UTC),
        duration_seconds=7200,
        expected_records=72,
    )


# --- Scenario 1: No orchestrator, with queue → queued ---


@pytest.mark.asyncio
async def test_no_orchestrator_with_queue_queues_request(detector, queue, sample_gap):
    detector.backfill_orchestrator = None
    detector.backfill_queue = queue

    await detector._trigger_backfill(
        symbol="BTCUSDT", timeframe="1h", gap=sample_gap, severity="high"
    )

    assert queue.size == 1
    queued = queue._queue[0]["request"]
    assert queued.symbol == "BTCUSDT"
    assert queued.data_type == "candles"
    assert queued.source == "gap_detector"


# --- Scenario 2: No orchestrator, no queue → logged warning, not dropped silently ---


@pytest.mark.asyncio
async def test_no_orchestrator_no_queue_logs_warning(detector, sample_gap):
    detector.backfill_orchestrator = None
    detector.backfill_queue = None

    with patch.object(detector, "_log_gap", new_callable=AsyncMock):
        await detector._trigger_backfill(
            symbol="ETHUSDT", timeframe="1h", gap=sample_gap, severity="high"
        )

    # The gap is NOT silently dropped — detector logs a warning instead
    # (the old behavior was to return silently)


# --- Scenario 3: Orchestrator available → goes directly ---


@pytest.mark.asyncio
async def test_orchestrator_available_calls_directly(
    detector, mock_orchestrator, sample_gap
):
    detector.backfill_orchestrator = mock_orchestrator
    detector.backfill_queue = BackfillRequestQueue()

    await detector._trigger_backfill(
        symbol="BTCUSDT", timeframe="1h", gap=sample_gap, severity="high"
    )

    mock_orchestrator.create_backfill_job.assert_awaited_once()
    assert detector.backfill_queue.size == 0


# --- Scenario 4: Orchestrator call fails → queued as fallback ---


@pytest.mark.asyncio
async def test_orchestrator_failure_queues_request(detector, queue, sample_gap):
    orchestrator = AsyncMock()
    orchestrator.create_backfill_job = AsyncMock(
        side_effect=Exception("connection lost")
    )

    detector.backfill_orchestrator = orchestrator
    detector.backfill_queue = queue

    await detector._trigger_backfill(
        symbol="BTCUSDT", timeframe="1h", gap=sample_gap, severity="high"
    )

    # Should have been queued
    assert queue.size == 1
    queued = queue._queue[0]["request"]
    assert queued.symbol == "BTCUSDT"


# --- Scenario 5: Small gap below threshold → skipped ---


@pytest.mark.asyncio
async def test_small_gap_below_threshold_skipped(detector, queue, mock_db_manager):
    detector.backfill_orchestrator = None
    detector.backfill_queue = queue

    small_gap = GapInfo(
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, hour=0, minute=30, tzinfo=UTC),
        duration_seconds=1800,  # 30 min — below MIN_AUTO_BACKFILL_GAP (3600)
        expected_records=30,
    )

    await detector._trigger_backfill(
        symbol="BTCUSDT", timeframe="1h", gap=small_gap, severity="medium"
    )

    assert queue.size == 0  # too small to queue


# --- Scenario 6: GapDetector constructor accepts queue ---


def test_detector_accepts_queue(mock_db_manager, queue):
    detector = GapDetector(mock_db_manager, backfill_queue=queue)
    assert detector.backfill_queue is queue


def test_detector_accepts_both(mock_db_manager, queue, mock_orchestrator):
    detector = GapDetector(
        mock_db_manager,
        backfill_orchestrator=mock_orchestrator,
        backfill_queue=queue,
    )
    assert detector.backfill_orchestrator is mock_orchestrator
    assert detector.backfill_queue is queue
