"""Tests for periodic MySQL server status collection."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.main import DataManagerApp
from data_manager.maintenance import mysql_server_status as status


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql == "SHOW GLOBAL STATUS":
            return _Result(
                [
                    {"Variable_name": "Threads_connected", "Value": "7"},
                    {"Variable_name": "Max_used_connections", "Value": "22"},
                    {"Variable_name": "Aborted_clients", "Value": "41"},
                    {"Variable_name": "Aborted_connects", "Value": "3"},
                ]
            )
        return _Result([{"Variable_name": "max_user_connections", "Value": "30"}])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_adapter_returns_stubbed_values_and_only_read_only_statements():
    connection = _Connection()
    adapter = MySQLAdapter(
        connection_string="mysql+pymysql://user:pass@host/db", role="serving"
    )
    adapter.engine = SimpleNamespace(connect=lambda: connection)
    adapter._connected = True

    values = adapter.get_server_status()

    assert values == {
        "Threads_connected": 7,
        "Max_used_connections": 22,
        "Aborted_clients": 41,
        "Aborted_connects": 3,
        "max_user_connections": 30,
    }
    assert connection.statements == ["SHOW GLOBAL STATUS", "SHOW VARIABLES"]


@pytest.mark.asyncio
async def test_collection_sets_five_gauges_and_runs_in_worker_thread():
    event_loop_thread = threading.get_ident()
    worker_thread: int | None = None
    stop_event = asyncio.Event()

    class Adapter:
        def get_server_status(self):
            nonlocal worker_thread
            worker_thread = threading.get_ident()
            stop_event.set()
            return {
                "Threads_connected": 7,
                "Max_used_connections": 22,
                "Aborted_clients": 41,
                "Aborted_connects": 3,
                "max_user_connections": 30,
            }

    await status.mysql_server_status_loop(lambda: Adapter(), stop_event)

    assert worker_thread != event_loop_thread
    assert status.mysql_server_threads_connected._value.get() == 7
    assert status.mysql_server_max_used_connections._value.get() == 22
    assert status.mysql_server_aborted_clients._value.get() == 41
    assert status.mysql_server_aborted_connects._value.get() == 3
    assert status.mysql_server_max_user_connections._value.get() == 30


@pytest.mark.asyncio
async def test_failure_increments_error_counter_without_raising():
    before = status.mysql_server_status_errors_total._value.get()
    stop_event = asyncio.Event()

    class Adapter:
        def get_server_status(self):
            stop_event.set()
            raise RuntimeError("database unavailable")

    await status.mysql_server_status_loop(lambda: Adapter(), stop_event)

    assert status.mysql_server_status_errors_total._value.get() == before + 1


def test_app_starts_collector_task_with_current_database_manager():
    app = DataManagerApp()
    app.db_manager = SimpleNamespace(mysql_adapter=object())
    task = object()

    with (
        patch(
            "data_manager.main.asyncio.create_task", return_value=task
        ) as create_task,
        patch.object(status, "mysql_server_status_loop") as collector,
    ):
        app._start_mysql_server_status_collector()

    assert app.mysql_server_status_task is task
    create_task.assert_called_once()
    collector.assert_called_once()


@pytest.mark.asyncio
async def test_app_stop_cancels_collector_before_database_shutdown():
    app = DataManagerApp()
    collector_task = asyncio.create_task(asyncio.sleep(60))
    app.mysql_server_status_task = collector_task
    app.running = True

    await app.stop()

    assert collector_task.cancelled()
