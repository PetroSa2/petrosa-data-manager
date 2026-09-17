"""Regression tests for petrosa-data-manager#312.

Covers the two root-caused defects from the ticket's "Scope / To do":

1. ``as_aware_utc()`` tz-normalization helper and its use in
   ``HealthScorer.calculate_health`` / ``GapDetector.detect_gaps`` --
   previously these raised ``TypeError: can't subtract/compare
   offset-naive and offset-aware datetimes`` whenever Mongo returned a
   naive ``datetime`` for a BSON date (which it always does).
2. ``BackfillOrchestrator.create_backfill_job`` -- previously wrote a
   *nested* ``job.model_dump()`` dict (with a ``request`` sub-object) into
   the *flat* ``backfill_jobs`` MySQL table, silently defaulting the NOT
   NULL ``data_type``/``symbol`` columns to empty strings and causing
   ``execute_backfill`` to log ``Unsupported data type: `` for every job.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.auditor.gap_detector import GapDetector
from data_manager.auditor.health_scorer import HealthScorer
from data_manager.backfiller.orchestrator import BackfillOrchestrator
from data_manager.models.events import BackfillRequest
from data_manager.utils.time_utils import as_aware_utc


class TestAsAwareUtc:
    def test_naive_datetime_gets_utc_tzinfo(self):
        naive = datetime(2026, 1, 1, 12, 0, 0)
        result = as_aware_utc(naive)
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_aware_datetime_passes_through_unchanged(self):
        aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert as_aware_utc(aware) == aware

    def test_iso_string_is_parsed_and_normalized(self):
        result = as_aware_utc("2026-01-01T12:00:00")
        assert result.tzinfo is not None
        assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_db_manager():
    manager = MagicMock()
    manager.mysql_adapter = MagicMock()
    manager.mongodb_adapter = MagicMock()
    return manager


class TestGapDetectorTzNormalization:
    @pytest.mark.asyncio
    async def test_detect_gaps_does_not_raise_on_naive_mongo_timestamps(self):
        """Mongo/PyMongo returns naive datetimes for BSON dates. Mixing them
        with the aware `start`/`end` bounds used to raise TypeError inside
        detect_gaps's own try/except, silently swallowing the error and
        returning [] -- i.e. gap detection produced no usable verdict.
        """
        db_manager = _make_db_manager()
        detector = GapDetector(db_manager)

        naive_candles = [
            {"timestamp": datetime(2026, 1, 1, 0, 0, 0)},  # naive!
            {"timestamp": datetime(2026, 1, 1, 0, 5, 0)},  # naive!
        ]
        detector.candle_repo.get_range = AsyncMock(return_value=naive_candles)
        detector.audit_repo.log_gap = AsyncMock(return_value=True)

        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        # Previously this returned [] after swallowing a TypeError; it must
        # now actually compute gaps against the (large) missing tail.
        gaps = await detector.detect_gaps("BTCUSDT", "5m", start, end)

        assert len(gaps) == 1
        assert gaps[0].start_time.tzinfo is not None
        assert gaps[0].end_time.tzinfo is not None

    @pytest.mark.asyncio
    async def test_detect_gaps_accepts_naive_caller_bounds(self):
        """Callers occasionally pass naive start/end; normalize those too."""
        db_manager = _make_db_manager()
        detector = GapDetector(db_manager)
        detector.candle_repo.get_range = AsyncMock(return_value=[])
        detector.audit_repo.log_gap = AsyncMock(return_value=True)

        naive_start = datetime(2026, 1, 1, 0, 0, 0)
        naive_end = datetime(2026, 1, 1, 1, 0, 0)

        gaps = await detector.detect_gaps("BTCUSDT", "5m", naive_start, naive_end)

        assert len(gaps) == 1
        assert gaps[0].start_time == as_aware_utc(naive_start)


class TestHealthScorerTzNormalization:
    @pytest.mark.asyncio
    async def test_calculate_health_does_not_raise_on_naive_mongo_timestamp(self):
        db_manager = _make_db_manager()
        scorer = HealthScorer(db_manager)

        # Naive "now" -- mirrors what PyMongo actually returns for a BSON
        # date written moments ago (no tzinfo, but the instant is recent).
        naive_recent = datetime.utcnow() - timedelta(seconds=30)
        scorer.candle_repo.count = AsyncMock(return_value=100)
        scorer.candle_repo.get_latest = AsyncMock(
            return_value=[{"timestamp": naive_recent}]
        )
        scorer.health_repo.insert = AsyncMock(return_value=True)

        metrics = await scorer.calculate_health("BTCUSDT", "5m", lookback_hours=24)

        # Previously this TypeError was caught by the method's own
        # except-block and the default (0.0 completeness, 1-day-stale)
        # metrics were returned instead of a real freshness computation.
        assert metrics.freshness_seconds < 120
        assert metrics.freshness_seconds >= 0


class TestBackfillOrchestratorFlattening:
    @pytest.mark.asyncio
    async def test_create_backfill_job_flattens_request_into_flat_record(self):
        """The `backfill_jobs` MySQL table is FLAT (job_id, symbol,
        data_type, timeframe, start_time, end_time, status, ...). Writing
        job.model_dump() verbatim nests those fields under `request`
        instead, so the table's NOT NULL `data_type`/`symbol` columns get
        silently defaulted to '' by MySQL -- reproduced by asserting the
        record handed to create_job() has flat top-level keys, not a
        nested `request` dict.
        """
        db_manager = _make_db_manager()
        orchestrator = BackfillOrchestrator(db_manager)
        orchestrator.backfill_repo.create_job = AsyncMock(return_value=True)

        request = BackfillRequest(
            symbol="BTCUSDT",
            data_type="candles",
            timeframe="5m",
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
            end_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )

        await orchestrator.create_backfill_job(request)

        orchestrator.backfill_repo.create_job.assert_called_once()
        record = orchestrator.backfill_repo.create_job.call_args[0][0]

        assert "request" not in record
        assert record["data_type"] == "candles"
        assert record["symbol"] == "BTCUSDT"
        assert record["timeframe"] == "5m"
        assert record["status"] == "pending"
