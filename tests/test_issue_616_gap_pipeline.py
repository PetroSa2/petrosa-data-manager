from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.auditor.gap_detector import GapDetector
from data_manager.backfiller.orchestrator import BackfillOrchestrator
from data_manager.models.events import BackfillRequest
from data_manager.models.health import GapInfo
from data_manager.services import backfill_queue
from data_manager.utils.time_utils import parse_timeframe_to_seconds


def _db():
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("timeframe", ["5m", "15m", "30m", "1h", "1d"])
async def test_last_closed_candle_has_no_tail_gap(timeframe):
    detector = GapDetector(_db())
    start = datetime(2026, 1, 1, tzinfo=UTC)
    interval = timedelta(seconds=parse_timeframe_to_seconds(timeframe))
    end = start + interval
    detector.candle_repo.get_range = AsyncMock(
        return_value=[{"timestamp": start}, {"timestamp": end}]
    )
    detector.audit_repo.log_gap = AsyncMock()

    assert await detector.detect_gaps("BTCUSDT", timeframe, start, end) == []
    detector.audit_repo.log_gap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeframe", ["5m", "15m", "30m", "1h", "1d"])
async def test_interior_gap_is_one_backfill(timeframe):
    detector = GapDetector(_db())
    orchestrator = MagicMock()
    orchestrator.create_backfill_job = AsyncMock(return_value=MagicMock(job_id="job"))
    detector.backfill_orchestrator = orchestrator
    start = datetime(2026, 1, 1, tzinfo=UTC)
    interval = timedelta(seconds=parse_timeframe_to_seconds(timeframe))
    end = start + interval * 3
    detector.candle_repo.get_range = AsyncMock(
        return_value=[{"timestamp": start}, {"timestamp": end}]
    )
    detector.audit_repo.log_gap = AsyncMock()

    with patch("constants.ENABLE_AUTO_BACKFILL", True):
        gaps = await detector.detect_gaps("BTCUSDT", timeframe, start, end)

    assert len(gaps) == 1
    assert gaps[0].start_time == start + interval
    assert gaps[0].end_time == end
    orchestrator.create_backfill_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_job_counts_empty_but_not_filled():
    orchestrator = BackfillOrchestrator.__new__(BackfillOrchestrator)
    orchestrator.backfill_repo = AsyncMock()
    orchestrator.backfill_repo.get_job.return_value = {
        "symbol": "BTCUSDT",
        "data_type": "candles",
        "timeframe": "5m",
        "start_time": datetime(2026, 1, 1, tzinfo=UTC),
        "end_time": datetime(2026, 1, 1, 1, tzinfo=UTC),
        "source": "gap_detector",
    }
    orchestrator._backfill_candles = AsyncMock(return_value=(0, 0, False))
    request = BackfillRequest(
        symbol="BTCUSDT",
        data_type="candles",
        timeframe="5m",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
        source="gap_detector",
    )
    backfill_queue.register_auto_backfill("job-1", request)

    filled = backfill_queue.gaps_filled_auto_total.labels(
        symbol="BTCUSDT", timeframe="5m"
    )
    empty = backfill_queue.backfill_jobs_empty_total.labels(
        symbol="BTCUSDT", timeframe="5m"
    )
    filled_before, empty_before = filled._value.get(), empty._value.get()

    await orchestrator.execute_backfill("job-1")

    assert filled._value.get() == filled_before
    assert empty._value.get() == empty_before + 1


@pytest.mark.asyncio
async def test_empty_range_cooldown_blocks_requeue_until_expiry(caplog):
    backfill_queue._empty_ranges.clear()
    request = BackfillRequest(
        symbol="BTCUSDT",
        data_type="candles",
        timeframe="5m",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    with patch("constants.BACKFILL_EMPTY_COOLDOWN_THRESHOLD", 3), patch(
        "constants.BACKFILL_EMPTY_COOLDOWN_SECONDS", 3600
    ):
        for _ in range(3):
            backfill_queue.record_empty_backfill(request)
        assert backfill_queue.empty_backfill_suppressed(request)
        assert caplog.text.count("empty-result cooldown") == 1

        detector = GapDetector(_db())
        detector.backfill_orchestrator = MagicMock()
        detector.backfill_orchestrator.create_backfill_job = AsyncMock()
        await detector._trigger_backfill(
            request.symbol,
            request.timeframe,
            GapInfo(
                start_time=request.start_time,
                end_time=request.end_time,
                duration_seconds=3600,
                expected_records=12,
            ),
            "high",
        )
        detector.backfill_orchestrator.create_backfill_job.assert_not_awaited()

        key = backfill_queue.backfill_range_key(request)
        backfill_queue._empty_ranges[key] = (3, datetime.now(UTC) - timedelta(seconds=1))
        assert not backfill_queue.empty_backfill_suppressed(request)

    backfill_queue._empty_ranges.clear()


@pytest.mark.asyncio
async def test_invalid_or_undersized_windows_are_not_queued():
    detector = GapDetector(_db())
    detector.backfill_orchestrator = MagicMock()
    detector.backfill_orchestrator.create_backfill_job = AsyncMock()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with patch("constants.MIN_AUTO_BACKFILL_GAP", 120):
        await detector._trigger_backfill(
            "BTCUSDT",
            "1d",
            GapInfo(
                start_time=now,
                end_time=now + timedelta(seconds=60),
                duration_seconds=60,
                expected_records=0,
            ),
            "medium",
        )
        await detector._trigger_backfill(
            "BTCUSDT",
            "1d",
            GapInfo(
                start_time=now,
                end_time=now,
                duration_seconds=0,
                expected_records=0,
            ),
            "medium",
        )

    detector.backfill_orchestrator.create_backfill_job.assert_not_awaited()
