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
    async def test_non_leader_pod_runs_no_cycle_while_not_leader(
        self, db_manager: MagicMock
    ):
        """It waits (re-checking each heartbeat) instead of returning; no writes."""
        election = _Election(leader=False)
        states: list[bool] = []
        sched = CandleWarmupScheduler(
            db_manager, leader_election=election, on_active=states.append
        )
        polls = _Script(sched, stop_after=3)

        with _enabled_consts():
            with patch.object(sched, "run_cycle", new=AsyncMock()) as cycle:
                with patch(f"{MODULE}.asyncio.sleep", new=polls):
                    await asyncio.wait_for(sched.start(), timeout=5)

        cycle.assert_not_awaited()
        assert polls.durations[1:] == [30.0, 30.0, 30.0]  # heartbeat polls
        assert states == [False]  # exported 0 at start, never set to 1

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


# --- petrosa-data-manager#848: wait for leadership, resume after losing it ---------


class _Election:
    def __init__(self, leader: bool) -> None:
        self.is_leader = leader
        self.heartbeat_interval = 30


class _Script:
    """Replaces asyncio.sleep: records durations, runs per-call hooks, stops the loop."""

    def __init__(self, sched, stop_after: int, hooks=None) -> None:
        self.sched = sched
        self.stop_after = stop_after
        self.hooks = hooks or {}
        self.durations: list[float] = []

    async def __call__(self, seconds) -> None:
        self.durations.append(float(seconds))
        call = len(self.durations)
        hook = self.hooks.get(call)
        if hook is not None:
            hook()
        if call > self.stop_after:
            await self.sched.stop()


def _enabled_consts(interval: int = 3600):
    consts = patch(f"{MODULE}.constants")
    mock = consts.start()
    mock.ENABLE_CANDLE_WARMUP_SCHEDULER = True
    mock.ENABLE_LEADER_ELECTION = True
    mock.CANDLE_WARMUP_SCHEDULER_INTERVAL = interval
    mock.CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY = 0
    mock.CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF = 10

    class _Ctx:
        def __enter__(self):
            return mock

        def __exit__(self, *exc):
            consts.stop()
            return False

    return _Ctx()


class TestWaitsForLeadership:
    @pytest.mark.asyncio
    async def test_becomes_active_when_leadership_arrives_after_startup(
        self, db_manager: MagicMock
    ):
        election = _Election(leader=False)
        states: list[bool] = []
        sched = CandleWarmupScheduler(
            db_manager, leader_election=election, on_active=states.append
        )

        def win():
            election.is_leader = True

        # sleep #1 = initial delay, #2 = first heartbeat poll -> wins the lease
        script = _Script(sched, stop_after=3, hooks={2: win})
        cycle = AsyncMock(return_value=[])
        with _enabled_consts():
            with patch.object(sched, "run_cycle", new=cycle):
                with patch(f"{MODULE}.asyncio.sleep", new=script):
                    await asyncio.wait_for(sched.start(), timeout=5)

        assert cycle.await_count == 1  # ran once leadership arrived, no restart
        assert states == [False, True, False]  # 0 at start, leader, stopped
        assert sched.running is False

    @pytest.mark.asyncio
    async def test_resumes_after_losing_and_regaining_leadership(
        self, db_manager: MagicMock
    ):
        election = _Election(leader=True)
        states: list[bool] = []
        sched = CandleWarmupScheduler(
            db_manager, leader_election=election, on_active=states.append
        )

        def lose():
            election.is_leader = False

        def regain():
            election.is_leader = True

        # 1 initial delay; cycle 1; 2 first chunk of the interval -> lease lost;
        # 3 heartbeat poll while follower -> lease regained; cycle 2; 4 chunk -> stop
        script = _Script(sched, stop_after=3, hooks={2: lose, 3: regain})
        cycle = AsyncMock(return_value=[])
        with _enabled_consts():
            with patch.object(sched, "run_cycle", new=cycle):
                with patch(f"{MODULE}.asyncio.sleep", new=script):
                    await asyncio.wait_for(sched.start(), timeout=5)

        assert cycle.await_count == 2
        assert states == [False, True, False, True, False]

    @pytest.mark.asyncio
    async def test_a_lost_lease_is_noticed_within_one_heartbeat(
        self, db_manager: MagicMock
    ):
        """The hour-long interval is slept in heartbeat chunks, not in one go."""
        election = _Election(leader=True)
        states: list[bool] = []
        sched = CandleWarmupScheduler(
            db_manager, leader_election=election, on_active=states.append
        )

        def lose():
            election.is_leader = False

        script = _Script(sched, stop_after=2, hooks={2: lose})
        with _enabled_consts(interval=3600):
            with patch.object(sched, "run_cycle", new=AsyncMock(return_value=[])):
                with patch(f"{MODULE}.asyncio.sleep", new=script):
                    await asyncio.wait_for(sched.start(), timeout=5)

        assert script.durations[1] == 30.0  # first chunk of the 3600 s interval
        assert states[:3] == [False, True, False]  # inactive right after that chunk

    @pytest.mark.asyncio
    async def test_two_pods_one_writer(self, db_manager: MagicMock):
        leader, follower = _Election(leader=True), _Election(leader=False)
        a_states: list[bool] = []
        b_states: list[bool] = []
        a = CandleWarmupScheduler(
            db_manager, leader_election=leader, on_active=a_states.append
        )
        b = CandleWarmupScheduler(
            db_manager, leader_election=follower, on_active=b_states.append
        )
        a_cycle, b_cycle = AsyncMock(return_value=[]), AsyncMock(return_value=[])

        with _enabled_consts():
            with (
                patch.object(a, "run_cycle", new=a_cycle),
                patch.object(b, "run_cycle", new=b_cycle),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new=_Script(a, stop_after=2)):
                    await asyncio.wait_for(a.start(), timeout=5)
                with patch(f"{MODULE}.asyncio.sleep", new=_Script(b, stop_after=2)):
                    await asyncio.wait_for(b.start(), timeout=5)

        assert a_cycle.await_count >= 1 and b_cycle.await_count == 0
        assert a_states[:2] == [False, True] and b_states == [False]  # follower reads 0

    @pytest.mark.asyncio
    async def test_failing_gauge_callback_never_stops_the_loop(
        self, db_manager: MagicMock
    ):
        def broken(_up):
            raise RuntimeError("otel down")

        sched = CandleWarmupScheduler(
            db_manager, leader_election=_Election(leader=True), on_active=broken
        )
        cycle = AsyncMock(return_value=[])
        with _enabled_consts():
            with patch.object(sched, "run_cycle", new=cycle):
                with patch(f"{MODULE}.asyncio.sleep", new=_Script(sched, stop_after=3)):
                    await asyncio.wait_for(sched.start(), timeout=5)
        assert cycle.await_count >= 1


class TestCancellation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("leader", [True, False])
    async def test_cancel_mid_sleep_propagates_and_leaves_the_gauge_at_zero(
        self, db_manager: MagicMock, leader: bool
    ):
        """A cancel is never swallowed: the task ends cancelled, the gauge at 0."""
        from data_manager.maintenance import candle_warmup_scheduler as module

        states: list[bool] = []
        sched = CandleWarmupScheduler(
            db_manager,
            leader_election=_Election(leader=leader),
            on_active=states.append,
        )
        asleep = asyncio.Event()
        calls: list[float] = []

        async def blocking_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:  # past the initial delay: the cycle wait / leader poll
                asleep.set()
                await asyncio.Event().wait()  # sleeps until cancelled

        with _enabled_consts():
            with patch.object(sched, "run_cycle", new=AsyncMock(return_value=[])):
                with patch(f"{MODULE}.asyncio.sleep", new=blocking_sleep):
                    task = asyncio.create_task(sched.start())
                    await asyncio.wait_for(asleep.wait(), timeout=5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

        assert task.cancelled() is True
        assert sched.running is False
        assert sched.active is False
        assert module.candle_backfill_active._value.get() == 0
        assert states == ([False, True, False] if leader else [False])
