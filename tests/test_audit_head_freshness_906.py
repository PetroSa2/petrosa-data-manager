"""petrosa-data-manager#906: the 15m audit flagged the not-yet-ingested newest candle as a gap.

Production 2026-11-26 18:45:5xZ: every symbol logged "Found 1 gaps ... 15m" with
"Gap from 18:15 to 18:30" and triggered a backfill, while a read at 19:2xZ showed 103
contiguous 15m candles. The 18:15 candle (closed 18:30) simply had not reached the store yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import constants
from data_manager.auditor.gap_detector import GapDetector


def _detector(timestamps, orchestrator=None):
    db = MagicMock()
    db.mysql_adapter = MagicMock()
    db.mongodb_adapter = MagicMock()
    detector = GapDetector(db, backfill_orchestrator=orchestrator)
    detector.candle_repo.get_range = AsyncMock(
        return_value=[{"timestamp": t} for t in timestamps]
    )
    detector.audit_repo.log_gap = AsyncMock()
    return detector


def _series(first: datetime, last: datetime, step: timedelta) -> list[datetime]:
    out, t = [], first
    while t <= last:
        out.append(t)
        t += step
    return out


NOW = datetime(2026, 11, 26, 18, 45, 50, tzinfo=UTC)
M15 = timedelta(minutes=15)


async def _audit(detector, now=NOW):
    end = datetime(
        2026, 11, 26, 18, 30, tzinfo=UTC
    )  # last_closed_candle_open(NOW, 15m)
    with (
        patch("data_manager.auditor.gap_detector.datetime") as clock,
        patch("constants.ENABLE_AUTO_BACKFILL", True),
    ):
        clock.now.return_value = now
        return await detector.detect_gaps(
            "XRPUSDT", "15m", end - timedelta(hours=24), end
        )


@pytest.mark.asyncio
async def test_newest_candle_still_arriving_is_not_a_gap_and_not_backfilled():
    """The production case: store holds candles up to 18:00 at 18:45:50 (18:15 and 18:30 pending)."""
    orchestrator = MagicMock()
    orchestrator.create_backfill_job = AsyncMock()
    start = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
    detector = _detector(
        _series(start, datetime(2026, 11, 26, 18, 0, tzinfo=UTC), M15), orchestrator
    )
    assert await _audit(detector) == []
    detector.audit_repo.log_gap.assert_not_awaited()
    orchestrator.create_backfill_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stalled_head_beyond_the_freshness_budget_is_still_a_gap():
    """Newest stored 17:30 at 18:45:50: past 3 x 15m, a real stall, flagged and repaired."""
    orchestrator = MagicMock()
    orchestrator.create_backfill_job = AsyncMock(return_value=MagicMock(job_id="j"))
    start = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
    detector = _detector(
        _series(start, datetime(2026, 11, 26, 17, 30, tzinfo=UTC), M15), orchestrator
    )
    gaps = await _audit(detector)
    assert len(gaps) == 1
    assert gaps[0].start_time == datetime(2026, 11, 26, 17, 45, tzinfo=UTC)
    assert gaps[0].end_time == datetime(2026, 11, 26, 18, 30, tzinfo=UTC)
    orchestrator.create_backfill_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_missing_closed_candle_inside_the_window_is_a_gap_and_repaired():
    """A genuine hole (12:00 missing between 11:45 and 12:15) is unaffected by the head grace."""
    orchestrator = MagicMock()
    orchestrator.create_backfill_job = AsyncMock(return_value=MagicMock(job_id="j"))
    start = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
    hole = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    series = [
        t
        for t in _series(start, datetime(2026, 11, 26, 18, 30, tzinfo=UTC), M15)
        if t != hole
    ]
    gaps = await _audit(_detector(series, orchestrator))
    assert [(g.start_time, g.end_time) for g in gaps] == [(hole, hole + M15)]
    orchestrator.create_backfill_job.assert_awaited_once()


def test_head_grace_follows_the_readiness_freshness_budget():
    assert constants.CANDLE_WARMUP_FRESHNESS_INTERVALS == 3.0
