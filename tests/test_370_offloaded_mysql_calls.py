"""petrosa-data-manager#370: synchronous MySQL calls must run off the event loop.

The API, the NATS consumers and the schedulers share one asyncio loop, so a
blocking MySQL call made directly in an ``async def`` stalls every request,
including ``/health`` and ``/openapi.json``. The fakes below record, for each
MySQL call, whether it ran on the event-loop thread. Code on that thread sees
a running loop; a worker thread started by ``asyncio.to_thread`` does not.
"""

import asyncio
import logging
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.routes import generic
from data_manager.db.database_manager import DatabaseManager
from data_manager.db.mysql_adapter import WriteResult

POSITION_COLUMNS = {
    "symbol",
    "side",
    "quantity",
    "status",
    "timestamp",
    "created_at",
    "updated_at",
}


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _dual_write_tasks_since(before: set[asyncio.Task]) -> set[asyncio.Task]:
    """Signals dual-write tasks created since ``before`` was snapshotted."""
    return set(generic._signal_dual_write_tasks) - before


class RecordingMySQLAdapter:
    """MySQL adapter double that records the thread each blocking call ran on.

    ``delay_s`` makes every blocking call sleep, like a slow MySQL, and
    ``started`` is set once any blocking call is in progress.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None, delay_s: float = 0):
        self.rows = rows or []
        self.delay_s = delay_s
        self.calls: list[tuple[str, bool]] = []
        self.started = threading.Event()

    def _blocking_call(self, name: str) -> None:
        self.calls.append((name, _on_event_loop_thread()))
        self.started.set()
        if self.delay_s:
            time.sleep(self.delay_s)

    def _matching(self, filter_: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in filter_.items())
        ]

    def write(self, model_instances: list[Any], collection: str) -> WriteResult:
        self._blocking_call("write")
        return WriteResult(inserted=len(model_instances), duplicates=0, failed=0)

    def update(self, collection: str, filter_: dict, data: dict) -> int:
        self._blocking_call("update")
        return len(self._matching(filter_))

    def delete(self, collection: str, filter_: dict) -> int:
        self._blocking_call("delete")
        return len(self._matching(filter_))

    def get_column_names(self, collection: str) -> set[str]:
        # May reflect the table on first use, so it is blocking I/O too.
        self._blocking_call("get_column_names")
        return POSITION_COLUMNS

    def called_on_loop(self) -> list[str]:
        return [name for name, on_loop in self.calls if on_loop]

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def mysql_adapter():
    return RecordingMySQLAdapter(
        rows=[
            {"symbol": "BTCUSDT", "side": "LONG", "quantity": 1.0},
            {"symbol": "ETHUSDT", "side": "SHORT", "quantity": 2.0},
        ]
    )


@pytest.fixture
def client(mock_db_manager, mysql_adapter):
    mock_db_manager.mysql_adapter = mysql_adapter
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None


def test_recording_adapter_detects_calls_on_the_event_loop():
    """Guard for the assertions below: a direct call from a coroutine is
    reported as on-loop, a call through asyncio.to_thread is not."""
    adapter = RecordingMySQLAdapter()

    async def exercise() -> None:
        adapter.write([], "positions")
        await asyncio.to_thread(adapter.write, [], "positions")

    asyncio.run(exercise())

    assert adapter.calls == [("write", True), ("write", False)]


class TestGenericMySQLRoutesRunOffLoop:
    def test_insert_writes_off_loop(self, client, mysql_adapter):
        response = client.post(
            "/api/v1/mysql/positions",
            json={"data": [{"symbol": "BTCUSDT", "side": "LONG", "quantity": 1.0}]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["inserted_count"] == 1
        assert mysql_adapter.call_names() == ["write"]
        assert mysql_adapter.called_on_loop() == []

    def test_update_existing_record_runs_off_loop(self, client, mysql_adapter):
        response = client.put(
            "/api/v1/mysql/positions",
            json={"filter": {"symbol": "BTCUSDT"}, "data": {"quantity": 3.0}},
        )

        assert response.status_code == 200, response.text
        assert response.json()["updated_count"] == 1
        assert response.json()["upserted"] is False
        assert mysql_adapter.call_names() == ["get_column_names", "update"]
        assert mysql_adapter.called_on_loop() == []

    def test_upsert_without_match_updates_then_writes_off_loop(
        self, client, mysql_adapter
    ):
        response = client.put(
            "/api/v1/mysql/positions",
            json={
                "filter": {"symbol": "SOLUSDT"},
                "data": {"symbol": "SOLUSDT", "quantity": 5.0},
                "upsert": True,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["updated_count"] == 1
        assert response.json()["upserted"] is True
        assert mysql_adapter.call_names() == ["get_column_names", "update", "write"]
        assert mysql_adapter.called_on_loop() == []

    def test_delete_runs_off_loop(self, client, mysql_adapter):
        response = client.request(
            "DELETE", "/api/v1/mysql/positions", json={"filter": {"side": "SHORT"}}
        )

        assert response.status_code == 200, response.text
        assert response.json()["deleted_count"] == 1
        assert mysql_adapter.call_names() == ["delete"]
        assert mysql_adapter.called_on_loop() == []

    def test_batch_insert_update_delete_run_off_loop(self, client, mysql_adapter):
        response = client.post(
            "/api/v1/mysql/positions/batch",
            json={
                "operations": [
                    {"type": "insert", "data": [{"symbol": "BNBUSDT"}]},
                    {
                        "type": "update",
                        "filter": {"symbol": "BTCUSDT"},
                        "data": {"quantity": 9.0},
                    },
                    {"type": "delete", "filter": {"symbol": "ETHUSDT"}},
                ]
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["results"] == [
            {"type": "insert", "count": 1},
            {"type": "update", "count": 1},
            {"type": "delete", "count": 1},
        ]
        # get_column_names is the up-front validation of the update operation.
        assert mysql_adapter.call_names() == [
            "get_column_names",
            "write",
            "update",
            "delete",
        ]
        assert mysql_adapter.called_on_loop() == []

    def test_signals_dual_write_runs_off_loop(self, client, mysql_adapter):
        response = client.post(
            "/api/v1/mongodb/signals",
            json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
        )

        assert response.status_code == 200, response.text
        assert mysql_adapter.call_names() == ["write"]
        assert mysql_adapter.called_on_loop() == []


class TestSignalsDualWriteTask:
    """_dual_write_signals_to_mysql schedules a tracked background task."""

    @pytest.fixture
    def db_manager(self, mysql_adapter, monkeypatch):
        manager = Mock()
        manager.mysql_adapter = mysql_adapter
        monkeypatch.setattr(api_module, "db_manager", manager)
        return manager

    @pytest.mark.asyncio
    async def test_task_is_retained_until_the_write_finishes(
        self, db_manager, mysql_adapter
    ):
        before = set(generic._signal_dual_write_tasks)
        generic._dual_write_signals_to_mysql([{"symbol": "BTCUSDT", "action": "buy"}])

        pending = _dual_write_tasks_since(before)
        assert len(pending) == 1  # strongly referenced while in flight
        await asyncio.gather(*pending)
        await asyncio.sleep(0)  # let the done-callback run

        assert pending.isdisjoint(generic._signal_dual_write_tasks)
        assert mysql_adapter.call_names() == ["write"]
        assert mysql_adapter.called_on_loop() == []

    @pytest.mark.asyncio
    async def test_write_failure_is_logged_not_raised(self, db_manager, caplog):
        caplog.set_level(logging.ERROR, logger=generic.logger.name)
        db_manager.mysql_adapter = Mock()
        db_manager.mysql_adapter.write.side_effect = RuntimeError("mysql down")

        before = set(generic._signal_dual_write_tasks)
        generic._dual_write_signals_to_mysql([{"symbol": "BTCUSDT", "action": "buy"}])
        (task,) = _dual_write_tasks_since(before)
        await task

        assert task.exception() is None  # no "Task exception was never retrieved"
        assert "MySQL signals dual-write failed" in caplog.text
        assert "mysql down" in caplog.text

    def test_without_running_loop_logs_and_skips(self, db_manager, caplog):
        caplog.set_level(logging.ERROR, logger=generic.logger.name)
        db_manager.mysql_adapter = Mock()

        generic._dual_write_signals_to_mysql([{"symbol": "BTCUSDT", "action": "buy"}])

        db_manager.mysql_adapter.write.assert_not_called()
        assert "no running event loop" in caplog.text


def _mysql_adapter_double(connect=None) -> MagicMock:
    adapter = MagicMock()
    adapter.is_connected = MagicMock(return_value=True)
    if connect is not None:
        adapter.connect.side_effect = connect
    return adapter


def _mongo_adapter_double() -> MagicMock:
    adapter = MagicMock()
    adapter.is_connected = MagicMock(return_value=True)
    adapter.ensure_indexes = AsyncMock()
    return adapter


class TestDatabaseManagerMySQLOffLoop:
    @pytest.mark.asyncio
    async def test_initialize_connects_mysql_off_loop(self):
        seen: list[bool] = []
        mysql = _mysql_adapter_double(
            connect=lambda: seen.append(_on_event_loop_thread())
        )
        with patch(
            "data_manager.db.database_manager.get_adapter",
            side_effect=[_mongo_adapter_double(), mysql],
        ):
            dm = DatabaseManager()
            await dm.initialize()
        try:
            assert seen == [False]
            assert dm.mysql_adapter is mysql
            assert dm._stats["mysql"]["connection_count"] == 1
        finally:
            await dm.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_mysql_off_loop(self):
        seen: list[bool] = []
        dm = DatabaseManager()
        dm.mysql_adapter = _mysql_adapter_double()
        dm.mysql_adapter.disconnect.side_effect = lambda: seen.append(
            _on_event_loop_thread()
        )

        await dm.shutdown()

        assert seen == [False]
        assert dm._stats["mysql"]["last_disconnected"] is not None

    @pytest.mark.asyncio
    async def test_reconnect_connects_off_loop_and_publishes_only_when_connected(
        self,
    ):
        connecting = threading.Event()
        release = threading.Event()
        seen: list[bool] = []

        def slow_connect() -> None:
            seen.append(_on_event_loop_thread())
            connecting.set()
            release.wait(5)

        new_mysql = _mysql_adapter_double(connect=slow_connect)
        with (
            patch(
                "data_manager.db.database_manager.get_adapter", return_value=new_mysql
            ),
            patch("data_manager.db.database_manager.constants") as const,
        ):
            const.DB_RECONNECT_MAX_ATTEMPTS = 5
            const.DB_RECONNECT_BACKOFF_BASE = 0.0  # 0.0 ** n == 0: no backoff wait
            dm = DatabaseManager()
            reconnect = asyncio.create_task(dm._reconnect_mysql())
            try:
                assert await asyncio.to_thread(connecting.wait, 5)
                # connect() is still running in its worker thread: the loop is
                # free, and callers still see no adapter rather than an
                # adapter whose tables are only half defined.
                assert dm.mysql_adapter is None
            finally:
                release.set()
            await asyncio.wait_for(reconnect, timeout=5)

        assert seen == [False]
        assert dm.mysql_adapter is new_mysql
        assert dm._mysql_reconnect_attempts == 0

    @pytest.mark.asyncio
    async def test_failed_reconnect_keeps_previous_adapter(self):
        def failing_connect() -> None:
            raise RuntimeError("still down")

        previous = _mysql_adapter_double()
        previous.is_connected.return_value = False
        with (
            patch(
                "data_manager.db.database_manager.get_adapter",
                return_value=_mysql_adapter_double(connect=failing_connect),
            ),
            patch("data_manager.db.database_manager.constants") as const,
        ):
            const.DB_RECONNECT_MAX_ATTEMPTS = 5
            const.DB_RECONNECT_BACKOFF_BASE = 0.0
            dm = DatabaseManager()
            dm.mysql_adapter = previous
            await dm._reconnect_mysql()

        assert dm.mysql_adapter is previous
        assert dm._stats["mysql"]["error_count"] == 1
        assert dm._mysql_reconnect_attempts == 1


@pytest.mark.asyncio
async def test_api_stays_responsive_while_mysql_work_is_in_flight(mock_db_manager):
    """Scope 3 of #370: with MySQL work in flight that takes 2 s per call, a
    DB-manager reconnect (the health-monitor path), a generic MySQL write and a
    signals insert with its MySQL dual-write, /health/liveness and
    /openapi.json still answer in under 1 s. Any of those MySQL calls made on
    the event loop would hold these requests for the full 2 s."""
    slow_mysql = RecordingMySQLAdapter(delay_s=2.0)
    mock_db_manager.mysql_adapter = slow_mysql
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager

    reconnect_started = threading.Event()

    def slow_connect() -> None:
        reconnect_started.set()
        time.sleep(2.0)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://dm") as http:
            # Build the OpenAPI schema once up front, as it is after the first
            # request in production, so the check below measures loop
            # responsiveness rather than one-off schema generation.
            assert (await http.get("/openapi.json")).status_code == 200

            with (
                patch(
                    "data_manager.db.database_manager.get_adapter",
                    return_value=_mysql_adapter_double(connect=slow_connect),
                ),
                patch("data_manager.db.database_manager.constants") as const,
            ):
                const.DB_RECONNECT_MAX_ATTEMPTS = 5
                const.DB_RECONNECT_BACKOFF_BASE = 0.0
                before = set(generic._signal_dual_write_tasks)
                t0 = time.monotonic()
                reconnect = asyncio.create_task(DatabaseManager()._reconnect_mysql())
                insert = asyncio.create_task(
                    http.post(
                        "/api/v1/mysql/positions",
                        json={"data": {"symbol": "BTCUSDT", "quantity": 1.0}},
                    )
                )
                signal = asyncio.create_task(
                    http.post(
                        "/api/v1/mongodb/signals",
                        json={"data": {"symbol": "BTCUSDT", "action": "buy"}},
                    )
                )
                assert await asyncio.to_thread(reconnect_started.wait, 5)
                assert await asyncio.to_thread(slow_mysql.started.wait, 5)

                for path in ("/health/liveness", "/openapi.json"):
                    t0 = time.monotonic()
                    response = await http.get(path)
                    elapsed = time.monotonic() - t0
                    assert response.status_code == 200, path
                    assert elapsed < 1.0, f"{path} took {elapsed:.2f}s"

                # All of the above took less than one slow MySQL call, and that
                # work is still running: nothing held the event loop. (A
                # blocking call on the loop would have finished before the
                # started events above could even be observed.)
                assert not reconnect.done()
                assert not insert.done()
                assert time.monotonic() - t0 < 1.0

                insert_response, signal_response = await asyncio.gather(insert, signal)
                await reconnect
                await asyncio.gather(*_dual_write_tasks_since(before))

        assert insert_response.status_code == 200
        assert signal_response.status_code == 200
        assert sorted(slow_mysql.call_names()) == ["write", "write"]
        assert slow_mysql.called_on_loop() == []
    finally:
        api_module.db_manager = None
