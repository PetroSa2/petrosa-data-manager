"""Tests for the in-memory backfill request queue (petrosa-data-manager#320)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.models.events import BackfillRequest
from data_manager.services.backfill_queue import (
    BackfillRequestQueue,
    backfill_queue_failed_total,
    backfill_queue_flushed_total,
    backfill_queue_size,
)


@pytest.fixture
def queue():
    return BackfillRequestQueue(max_size=5, max_retries=2)


@pytest.fixture
def mock_orchestrator():
    orchestrator = AsyncMock()
    orchestrator.create_backfill_job = AsyncMock(
        return_value=MagicMock(job_id="job-123")
    )
    return orchestrator


@pytest.fixture
def sample_request():
    return BackfillRequest(
        symbol="BTCUSDT",
        data_type="candles",
        timeframe="1h",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 2, tzinfo=UTC),
        priority=1,
        source="test",
    )


# --- Queue basics ---


@pytest.mark.asyncio
async def test_add_increments_size(queue, sample_request):
    assert queue.size == 0
    await queue.add(sample_request)
    assert queue.size == 1


@pytest.mark.asyncio
async def test_add_multiple(queue, sample_request):
    for i in range(3):
        req = sample_request.model_copy()
        req.symbol = f"SYMBOL{i}"
        await queue.add(req)
    assert queue.size == 3


@pytest.mark.asyncio
async def test_add_respects_max_size(queue, sample_request):
    # Fill to max
    for i in range(5):
        req = sample_request.model_copy()
        req.symbol = f"SYM{i}"
        await queue.add(req)
    assert queue.size == 5

    # Add one more — should drop oldest
    old_symbol = list(queue._queue)[0]["request"].symbol
    newer = sample_request.model_copy()
    newer.symbol = "NEW"
    await queue.add(newer)

    assert queue.size == 5
    assert old_symbol not in [item["request"].symbol for item in queue._queue]


# --- Flush ---


@pytest.mark.asyncio
async def test_flush_all_succeeds(queue, sample_request, mock_orchestrator):
    await queue.add(sample_request)
    flushed = await queue.flush(mock_orchestrator)
    assert flushed == 1
    assert queue.size == 0
    mock_orchestrator.create_backfill_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_flush_empty_queue(queue, mock_orchestrator):
    flushed = await queue.flush(mock_orchestrator)
    assert flushed == 0


@pytest.mark.asyncio
async def test_flush_without_orchestrator(queue, sample_request):
    await queue.add(sample_request)
    flushed = await queue.flush(None)
    assert flushed == 0
    assert queue.size == 1  # still queued


@pytest.mark.asyncio
async def test_flush_retry_on_failure(queue, sample_request):
    orchestrator = AsyncMock()
    # Fail first call, succeed second
    orchestrator.create_backfill_job = AsyncMock(
        side_effect=[Exception("fail"), MagicMock(job_id="job-456")]
    )

    await queue.add(sample_request)
    # First flush fails
    flushed = await queue.flush(orchestrator)
    assert flushed == 0
    assert queue.size == 1  # re-queued

    # Second flush succeeds
    flushed = await queue.flush(orchestrator)
    assert flushed == 1
    assert queue.size == 0


@pytest.mark.asyncio
async def test_flush_exhausts_retries(queue, sample_request):
    orchestrator = AsyncMock()
    orchestrator.create_backfill_job = AsyncMock(side_effect=Exception("always fail"))

    await queue.add(sample_request)
    # Flush twice (max_retries=2, so it survives 2 failures)
    await queue.flush(orchestrator)
    assert queue.size == 1  # still there, retries=1

    await queue.flush(orchestrator)
    assert queue.size == 0  # dropped after exceeding retries


# --- Status ---


@pytest.mark.asyncio
async def test_get_status(queue, sample_request):
    await queue.add(sample_request)
    status = await queue.get_status()
    assert status["size"] == 1
    assert status["max_size"] == 5
    assert status["oldest_age_seconds"] is not None
    assert status["newest_age_seconds"] is not None
