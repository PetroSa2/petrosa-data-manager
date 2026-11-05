"""A startup failure never leaves DB-dependent components off for the pod's lifetime (#829)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import data_manager.main as main_module
from data_manager.main import DataManagerApp


class _Gauge:
    def __init__(self) -> None:
        self.values: list[tuple[int, dict]] = []

    def set(self, value, attributes):
        self.values.append((value, attributes))

    def last(self, component: str) -> int | None:
        for value, attributes in reversed(self.values):
            if attributes == {"component": component}:
                return value
        return None


@pytest.fixture
def gauge(monkeypatch):
    recorder = _Gauge()
    monkeypatch.setattr(main_module, "STARTUP_COMPONENT_UP", recorder)
    return recorder


@pytest.mark.asyncio
async def test_ttl_coverage_failure_is_reported_and_keeps_the_database(gauge, caplog):
    app = DataManagerApp()
    manager = MagicMock()
    app.db_manager = manager
    app._ensure_mongodb_indexes = AsyncMock(
        side_effect=RuntimeError(
            "MongoDB collection x.new_one has no registered TTL policy"
        )
    )

    with caplog.at_level("ERROR"):
        await app._ensure_mongodb_indexes_or_report()

    assert app.db_manager is manager
    assert gauge.last("mongodb_ttl_coverage") == 0
    assert "new_one has no registered TTL policy" in caplog.text

    app._ensure_mongodb_indexes = AsyncMock()
    await app._ensure_mongodb_indexes_or_report()
    assert gauge.last("mongodb_ttl_coverage") == 1


@pytest.mark.asyncio
async def test_coordination_starts_once_across_startup_and_retry(gauge, monkeypatch):
    monkeypatch.setattr(main_module.constants, "ENABLE_LEADER_ELECTION", True)
    monkeypatch.setattr(main_module.constants, "ENABLE_REPORT_PRECOMPUTE", True)
    import data_manager.api.app as api_app

    # _start_coordination publishes the precomputer on the API module; restore it after the test.
    monkeypatch.setattr(api_app, "report_precomputer", None)
    app = DataManagerApp()
    app._set_leader_election_health_reference = MagicMock()
    election = MagicMock(is_leader=True, pod_id="pod-a")
    election.initialize = AsyncMock()
    election.start = AsyncMock()
    precomputer = MagicMock()
    precomputer.start = AsyncMock()

    with (
        patch(
            "data_manager.leader_election.LeaderElectionManager", return_value=election
        ) as election_cls,
        patch(
            "data_manager.services.report_precompute.ReportPrecomputer",
            return_value=precomputer,
        ) as precomputer_cls,
    ):
        await app._start_coordination()  # no database yet: nothing starts
        assert election_cls.call_count == 0
        app.db_manager = MagicMock()
        await app._start_coordination()
        await app._start_coordination()

    assert election_cls.call_count == 1
    assert election.start.await_count == 1
    assert precomputer_cls.call_count == 1
    assert precomputer.start.await_count == 1
    assert app.report_precomputer is precomputer
    assert gauge.last("leader_election") == 1
    assert gauge.last("report_precompute") == 1


@pytest.mark.asyncio
async def test_retry_runs_index_check_and_coordination(gauge):
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    manager = MagicMock()
    manager.initialize = AsyncMock()
    app._start_mongo_data_size_loop = MagicMock()
    app._check_schema_migrations = AsyncMock()
    app._ensure_mongodb_indexes_or_report = AsyncMock()
    app._start_database_consumers_after_retry = AsyncMock()
    app._start_coordination = AsyncMock()

    with (
        patch("data_manager.main.DatabaseManager", return_value=manager),
        patch("data_manager.main.asyncio.sleep", new=AsyncMock()),
    ):
        await app._retry_database_initialization()

    assert app.db_manager is manager
    app._ensure_mongodb_indexes_or_report.assert_awaited_once()
    app._start_coordination.assert_awaited_once()


@pytest.mark.asyncio
async def test_analytics_waits_for_the_retry_then_starts_once(gauge, monkeypatch):
    monkeypatch.setattr(main_module.constants, "ENABLE_ANALYTICS", True)
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    scheduler = MagicMock()
    scheduler.start = AsyncMock()
    manager = MagicMock()
    manager.mongo_healthy.return_value = True

    with patch(
        "data_manager.analytics.scheduler.AnalyticsScheduler", return_value=scheduler
    ) as scheduler_cls:
        runner = asyncio.create_task(app._run_analytics())
        await asyncio.sleep(0.05)
        assert scheduler_cls.call_count == 0
        assert gauge.last("analytics") == 0
        app.db_manager = manager  # the retry path publishes the database
        await asyncio.wait_for(runner, timeout=3)

    scheduler_cls.assert_called_once_with(manager)
    assert scheduler.start.await_count == 1
    analytics = [
        value for value, attrs in gauge.values if attrs == {"component": "analytics"}
    ]
    # 0 while waiting, 1 while the scheduler runs, 0 once it has stopped.
    assert analytics == [0, 1, 0]


@pytest.mark.asyncio
async def test_unhealthy_database_never_reports_analytics_up(gauge, monkeypatch):
    monkeypatch.setattr(main_module.constants, "ENABLE_ANALYTICS", True)
    app = DataManagerApp()
    app.db_manager = MagicMock()
    app.db_manager.mongo_healthy.return_value = False

    await app._run_analytics()

    assert [v for v, a in gauge.values if a == {"component": "analytics"}] == [0]


@pytest.mark.asyncio
async def test_runners_wait_while_retry_coordination_is_pending(gauge):
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    app.db_manager = MagicMock()
    app._coordination_pending = True
    waiter = asyncio.create_task(app._wait_for_database("candle_warmup"))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    app._coordination_pending = False
    assert await asyncio.wait_for(waiter, timeout=3) is True


@pytest.mark.asyncio
async def test_precompute_failure_in_retry_never_raises_or_reinitializes(
    gauge, monkeypatch
):
    monkeypatch.setattr(main_module.constants, "ENABLE_LEADER_ELECTION", False)
    monkeypatch.setattr(main_module.constants, "ENABLE_REPORT_PRECOMPUTE", True)
    import data_manager.api.app as api_app

    monkeypatch.setattr(api_app, "report_precomputer", None)
    monkeypatch.setattr(api_app, "db_manager", None)
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    manager = MagicMock()
    manager.initialize = AsyncMock()
    app._start_mongo_data_size_loop = MagicMock()
    app._check_schema_migrations = AsyncMock()
    app._ensure_mongodb_indexes_or_report = AsyncMock()
    precomputer = MagicMock()
    precomputer.start = AsyncMock(side_effect=RuntimeError("index create failed"))

    with (
        patch("data_manager.main.DatabaseManager", return_value=manager) as manager_cls,
        patch("data_manager.main.IntentConsumer") as intent,
        patch("data_manager.main.DecisionConsumer") as decision,
        patch("data_manager.main.AlertDispatcher") as alerts,
        patch("data_manager.main.ExecutionEventsConsumer") as execution,
        patch("data_manager.main.PnlConsumer") as pnl,
        patch(
            "data_manager.services.report_precompute.ReportPrecomputer",
            return_value=precomputer,
        ),
        patch("data_manager.main.asyncio.sleep", new=AsyncMock()),
    ):
        for consumer in (intent, decision, alerts, execution, pnl):
            consumer.return_value.start = AsyncMock(return_value=True)
        await app._retry_database_initialization()

    assert manager_cls.call_count == 1
    assert manager.initialize.await_count == 1
    assert app.db_manager is manager
    for consumer in (intent, decision, execution):
        assert consumer.return_value.start.await_count == 1
    assert app.report_precomputer is None
    assert api_app.report_precomputer is None
    assert gauge.last("report_precompute") == 0
    assert app._coordination_pending is False


@pytest.mark.asyncio
async def test_wait_for_database_ends_on_shutdown(gauge):
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    waiter = asyncio.create_task(app._wait_for_database("auditor"))
    await asyncio.sleep(0.05)
    app._shutdown_event.set()
    assert await asyncio.wait_for(waiter, timeout=3) is False
    assert gauge.last("auditor") == 0


@pytest.mark.asyncio
async def test_lazy_gap_backfill_builds_detector_once_database_exists():
    app = DataManagerApp()
    detector = MagicMock()
    detector.request_backfill = AsyncMock()
    backfill = app._lazy_daily_gap_backfill()

    assert await backfill("BTCUSDT", "1h", "gap", "high") is None

    app.db_manager = MagicMock()
    with patch.object(
        app, "_create_daily_gap_detector", return_value=detector
    ) as create:
        await backfill("BTCUSDT", "1h", "gap", "high")
        await backfill("ETHUSDT", "1h", "gap", "low")

    create.assert_called_once_with()
    assert detector.request_backfill.await_count == 2


@pytest.mark.asyncio
async def test_retry_failure_after_publish_never_releases_waiters_on_that_manager(
    gauge, monkeypatch
):
    import data_manager.api.app as api_app

    monkeypatch.setattr(api_app, "db_manager", None)
    app = DataManagerApp()
    app._db_retry_base_seconds = 0
    first, second = MagicMock(name="first"), MagicMock(name="second")
    for manager in (first, second):
        manager.initialize = AsyncMock()
        manager.shutdown = AsyncMock()
    app._start_mongo_data_size_loop = MagicMock()
    app._check_schema_migrations = AsyncMock(
        side_effect=[RuntimeError("migration check failed"), None]
    )
    app._ensure_mongodb_indexes_or_report = AsyncMock()
    app._start_database_consumers_after_retry = AsyncMock()
    app._start_coordination = AsyncMock()
    seen: list[object] = []

    async def waiter() -> None:
        assert await app._wait_for_database("auditor")
        seen.append(app.db_manager)

    task = asyncio.create_task(waiter())
    real_sleep = asyncio.sleep

    async def yielding_sleep(_delay):
        await real_sleep(0)

    with (
        patch("data_manager.main.DatabaseManager", side_effect=[first, second]),
        patch("data_manager.main.asyncio.sleep", new=yielding_sleep),
    ):
        await app._retry_database_initialization()
        await asyncio.wait_for(task, timeout=3)

    assert seen == [second]
    assert app.db_manager is second
    first.shutdown.assert_awaited_once()
    second.shutdown.assert_not_awaited()
    assert app._coordination_pending is False
