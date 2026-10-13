import asyncio
import importlib
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from data_manager.services.ledger_tieout_scheduler import LedgerTieoutScheduler


def _result():
    return {
        "days": [
            {"day": "2026-10-10", "unexplained": "1.25"},
            {"day": "2026-10-11", "unexplained": "-2.5"},
            {"day": "2026-10-12", "unexplained": "0.5"},
            {"day": "2026-10-13", "unexplained": "3.0"},
        ],
        "cumulative_variance": "2.25",
    }


@pytest.mark.asyncio
async def test_cycle_uses_lookback_range_and_sets_gauges():
    repository = MagicMock()
    repository.tieout.return_value = _result()
    repository.positions_tieout.return_value = {"phantom_rows": [{"id": 1}]}
    scheduler = LedgerTieoutScheduler(
        repository,
        now=lambda: datetime(2026, 10, 13, 15, tzinfo=UTC),
    )

    with (
        patch(
            "data_manager.services.ledger_tieout_scheduler.TIEOUT_VARIANCE"
        ) as variance,
        patch(
            "data_manager.services.ledger_tieout_scheduler.TIEOUT_CUMULATIVE"
        ) as cumulative,
        patch(
            "data_manager.services.ledger_tieout_scheduler.TIEOUT_LAST_COMPLETE_DAY"
        ) as last_day,
        patch(
            "data_manager.services.ledger_tieout_scheduler.OPEN_POSITION_VARIANCE"
        ) as positions,
    ):
        await scheduler.run_cycle()

    repository.tieout.assert_called_once_with(
        datetime(2026, 10, 10, tzinfo=UTC).date(),
        datetime(2026, 10, 13, tzinfo=UTC).date(),
    )
    variance.labels.assert_any_call(window="latest")
    variance.labels.assert_any_call(window="worst_in_window")
    cumulative.set.assert_called_once_with(2.25)
    last_day.set.assert_called_once()
    positions.set.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_non_leader_does_not_run_cycle():
    repository = MagicMock()
    scheduler = LedgerTieoutScheduler(
        repository, leader_election=MagicMock(is_leader=False)
    )

    assert await scheduler.run_cycle() is None
    repository.tieout.assert_not_called()
    repository.positions_tieout.assert_not_called()


@pytest.mark.asyncio
async def test_failure_isolated_and_next_run_succeeds(caplog):
    repository = MagicMock()
    repository.positions_tieout.return_value = {"phantom_rows": []}
    scheduler = LedgerTieoutScheduler(repository)
    scheduler.interval_seconds = 0.001
    scheduler.running = True
    calls = 0

    def tieout(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        scheduler.running = False
        return _result()

    repository.tieout.side_effect = tieout

    with patch("data_manager.services.ledger_tieout_scheduler.ERRORS_TOTAL") as errors:
        await asyncio.wait_for(scheduler._loop(), timeout=1)

    errors.inc.assert_called_once()
    assert "ledger_tieout_scheduler_cycle_failed" in caplog.text
    assert repository.tieout.call_count == 2


@pytest.mark.asyncio
async def test_repository_calls_run_off_event_loop():
    repository = MagicMock()
    loop_thread = threading.get_ident()
    worker_threads = []

    def tieout(first, last):
        worker_threads.append(threading.get_ident())
        return _result()

    def positions_tieout():
        worker_threads.append(threading.get_ident())
        return {"phantom_rows": []}

    repository.tieout.side_effect = tieout
    repository.positions_tieout.side_effect = positions_tieout
    await LedgerTieoutScheduler(repository).run_cycle()

    assert worker_threads
    assert all(thread_id != loop_thread for thread_id in worker_threads)


def test_environment_defaults_and_disablement(monkeypatch):
    import constants

    for name in (
        "LEDGER_TIEOUT_SCHEDULER_ENABLED",
        "LEDGER_TIEOUT_INTERVAL_SECONDS",
        "LEDGER_TIEOUT_LOOKBACK_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(constants)
    scheduler = LedgerTieoutScheduler(MagicMock())
    assert scheduler.enabled is True
    assert scheduler.interval_seconds == 3600
    assert scheduler.lookback_days == 3

    monkeypatch.setenv("LEDGER_TIEOUT_SCHEDULER_ENABLED", "false")
    importlib.reload(constants)
    assert LedgerTieoutScheduler(MagicMock()).enabled is False
    monkeypatch.delenv("LEDGER_TIEOUT_SCHEDULER_ENABLED")
    importlib.reload(constants)


@pytest.mark.asyncio
async def test_disabled_scheduler_does_not_start():
    with patch("data_manager.services.ledger_tieout_scheduler.constants") as constants:
        constants.LEDGER_TIEOUT_SCHEDULER_ENABLED = False
        constants.LEDGER_TIEOUT_INTERVAL_SECONDS = 3600
        constants.LEDGER_TIEOUT_LOOKBACK_DAYS = 3
        scheduler = LedgerTieoutScheduler(MagicMock())
        await scheduler.start()
        assert scheduler._task is None
