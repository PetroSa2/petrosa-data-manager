"""
Tests for the continuous candle warm-up backfill scheduler
(petrosa-data-manager#319).

Covers the five behaviours the ticket actually buys us:

1. The scheduler is **off** unless explicitly enabled (no surprise Mongo
   writer).
2. A cycle delegates to the #275 ``run_backfill`` rather than reimplementing
   it, and records the AC4 counter per collection.
3. Collections that pass the readiness gate are counted as ``skipped`` — the
   AC3 optimization is observable, not just implied.
4. Failures are surfaced (``failed`` outcome, ``consecutive_failures``,
   no ``last_success`` bump) so the AC5 alert has something to fire on.
5. Leadership is respected fail-closed, so N replicas never double-write.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.maintenance.candle_warmup_backfill import BackfillResult
from data_manager.maintenance.candle_warmup_scheduler import (
    CandleWarmupScheduler,
    _outcome,
    candle_backfill_collections_total,
    candle_backfill_last_success_timestamp,
)

MODULE = "data_manager.maintenance.candle_warmup_scheduler"


def _result(
    pair: str = "BTCUSDT",
    timeframe: str = "5m",
    *,
    written: int = 0,
    skipped: bool = False,
    error: str | None = None,
    source_rows: int = 0,
) -> BackfillResult:
    return BackfillResult(
        pair=pair,
        timeframe=timeframe,
        collection=f"candles_{pair.lower()}_{timeframe}",
        source_table=f"klines_{timeframe}",
        written=written,
        skipped=skipped,
        error=error,
        source_rows=source_rows,
    )


def _counter_value(timeframe: str, outcome: str) -> float:
    return candle_backfill_collections_total.labels(
        timeframe=timeframe, outcome=outcome
    )._value.get()


@pytest.fixture
def db_manager() -> MagicMock:
    manager = MagicMock()
    manager.mysql_adapter = MagicMock()
    manager.mongodb_adapter = MagicMock()
    manager.is_healthy = MagicMock(return_value=True)
    return manager


@pytest.fixture
def scheduler(db_manager: MagicMock) -> CandleWarmupScheduler:
    return CandleWarmupScheduler(db_manager)


class TestOutcomeClassification:
    """``_outcome`` is what the AC4 counter's label actually means."""

    def test_error_wins_over_everything(self):
        assert _outcome(_result(written=10, skipped=True, error="boom")) == "failed"

    def test_already_warm_is_skipped(self):
        assert _outcome(_result(skipped=True)) == "skipped"

    def test_wrote_candles_is_written(self):
        assert _outcome(_result(written=42)) == "written"

    def test_no_source_rows_is_not_reported_as_skipped(self):
        """A dry collection is unhealthy; conflating it with 'already warm'
        would hide the exact staleness #319 exists to catch."""
        assert _outcome(_result(written=0)) == "no_source_data"


class TestRunCycle:
    @pytest.mark.asyncio
    async def test_delegates_to_run_backfill_and_counts_collections(
        self, scheduler: CandleWarmupScheduler
    ):
        results = [
            _result("BTCUSDT", "1h", written=120),
            _result("ETHUSDT", "1h", skipped=True),
        ]
        before_written = _counter_value("1h", "written")
        before_skipped = _counter_value("1h", "skipped")

        with patch(f"{MODULE}.run_backfill", new=AsyncMock(return_value=results)) as rb:
            returned = await scheduler.run_cycle()

        rb.assert_awaited_once()
        # The existing #275 job is the one doing the work — the scheduler
        # passes it the live adapters and nothing else.
        await_args = rb.await_args
        assert await_args is not None
        assert await_args.args[0] is scheduler.db_manager.mysql_adapter
        assert await_args.args[1] is scheduler.db_manager.mongodb_adapter

        assert returned == results
        assert _counter_value("1h", "written") == before_written + 1
        assert _counter_value("1h", "skipped") == before_skipped + 1
        assert scheduler.cycles_run == 1

    @pytest.mark.asyncio
    async def test_clean_cycle_records_success_timestamp(
        self, scheduler: CandleWarmupScheduler
    ):
        with patch(
            f"{MODULE}.run_backfill",
            new=AsyncMock(return_value=[_result(written=5)]),
        ):
            await scheduler.run_cycle()

        assert scheduler.consecutive_failures == 0
        assert scheduler.last_success_time is not None
        assert candle_backfill_last_success_timestamp._value.get() > 0

    @pytest.mark.asyncio
    async def test_failed_collection_does_not_bump_last_success(
        self, scheduler: CandleWarmupScheduler
    ):
        before_failed = _counter_value("15m", "failed")

        with patch(
            f"{MODULE}.run_backfill",
            new=AsyncMock(
                return_value=[_result(timeframe="15m", error="mongo unreachable")]
            ),
        ):
            await scheduler.run_cycle()

        assert scheduler.consecutive_failures == 1
        assert scheduler.last_success_time is None
        assert _counter_value("15m", "failed") == before_failed + 1

    @pytest.mark.asyncio
    async def test_missing_adapters_raise_rather_than_silently_noop(
        self, db_manager: MagicMock
    ):
        db_manager.mongodb_adapter = None
        sched = CandleWarmupScheduler(db_manager)

        with pytest.raises(RuntimeError, match="mongodb_adapter"):
            await sched.run_cycle()


class TestStartLoop:
    @pytest.mark.asyncio
    async def test_disabled_by_default_runs_no_cycle(
        self, scheduler: CandleWarmupScheduler
    ):
        with patch(f"{MODULE}.constants") as consts:
            consts.ENABLE_CANDLE_WARMUP_SCHEDULER = False
            with patch.object(scheduler, "run_cycle", new=AsyncMock()) as cycle:
                await scheduler.start()

        cycle.assert_not_awaited()
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_enabled_loop_runs_cycles_until_stopped(
        self, scheduler: CandleWarmupScheduler
    ):
        calls: list[int] = []

        async def _cycle():
            calls.append(1)
            if len(calls) >= 2:
                await scheduler.stop()
            return []

        with patch(f"{MODULE}.constants") as consts:
            consts.ENABLE_CANDLE_WARMUP_SCHEDULER = True
            consts.ENABLE_LEADER_ELECTION = False
            consts.CANDLE_WARMUP_SCHEDULER_INTERVAL = 60
            consts.CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY = 0
            consts.CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF = 10

            with patch.object(
                scheduler, "run_cycle", new=AsyncMock(side_effect=_cycle)
            ):
                with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                    await asyncio.wait_for(scheduler.start(), timeout=5)

        assert len(calls) == 2
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_cycle_exception_does_not_kill_the_loop(
        self, scheduler: CandleWarmupScheduler
    ):
        attempts: list[int] = []

        async def _cycle():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("mysql down")
            await scheduler.stop()
            return []

        with patch(f"{MODULE}.constants") as consts:
            consts.ENABLE_CANDLE_WARMUP_SCHEDULER = True
            consts.ENABLE_LEADER_ELECTION = False
            consts.CANDLE_WARMUP_SCHEDULER_INTERVAL = 60
            consts.CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY = 0
            consts.CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF = 10

            with patch.object(
                scheduler, "run_cycle", new=AsyncMock(side_effect=_cycle)
            ):
                with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                    await asyncio.wait_for(scheduler.start(), timeout=5)

        # Survived the first failure and ran again.
        assert len(attempts) == 2
        assert scheduler.consecutive_failures == 1


class TestLeadership:
    @pytest.mark.asyncio
    async def test_non_leader_pod_does_not_run(self, db_manager: MagicMock):
        election = MagicMock()
        election.is_leader = False
        sched = CandleWarmupScheduler(db_manager, leader_election=election)

        with patch(f"{MODULE}.constants") as consts:
            consts.ENABLE_CANDLE_WARMUP_SCHEDULER = True
            consts.ENABLE_LEADER_ELECTION = True
            with patch.object(sched, "run_cycle", new=AsyncMock()) as cycle:
                await sched.start()

        cycle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_election_enabled_without_manager_fails_closed(
        self, db_manager: MagicMock
    ):
        """No proof of single-writership means no writes at all."""
        sched = CandleWarmupScheduler(db_manager, leader_election=None)

        with patch(f"{MODULE}.constants") as consts:
            consts.ENABLE_CANDLE_WARMUP_SCHEDULER = True
            consts.ENABLE_LEADER_ELECTION = True
            assert sched._is_leader() is False


class TestStatus:
    def test_status_exposes_failure_and_cycle_state(
        self, scheduler: CandleWarmupScheduler
    ):
        scheduler.cycles_run = 3
        scheduler.consecutive_failures = 2
        status = scheduler.get_status()

        assert status["cycles_run"] == 3
        assert status["consecutive_failures"] == 2
        assert status["last_cycle_time"] is None
        assert "interval_seconds" in status
