import asyncio
import threading
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mongomock_motor import AsyncMongoMockClient
from pydantic import ValidationError
from sqlalchemy.exc import NoSuchTableError

from data_manager.api.routes import trading_state as module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.repositories.trading_state_repository import TradingStateRepository
from data_manager.utils.circuit_breaker import CircuitBreakerOpenError


class MySQL:
    def __init__(self, fallback_error=None):
        self.unavailable = True
        self.fallback_error = fallback_error
        self.rows = {}
        self.versions = {}
        self.attempts = 0
        self.effects = 0

    def get_column_names(self, table):
        return {
            "position_id",
            "status",
            "risk_order_id",
            "exit_price",
            "date",
            "daily_pnl",
        }

    def persist_trading_state(self, table, data, version):
        self.attempts += 1
        if self.unavailable:
            raise CircuitBreakerOpenError("mysql", 60)
        key = (table, data.get("position_id", data.get("date")))
        if self.versions.get(key, 0) < version:
            self.rows[key] = deepcopy(data)
            self.versions[key] = version
            self.effects += 1

    def write(self, rows, table):
        assert table == "trading_state_mysql_dead_letters"
        assert rows[0].model_dump(mode="json")["payload"]["_mysql_copy_version"] >= 1
        if self.fallback_error:
            raise self.fallback_error
        return 1


@pytest.fixture
def recovery(monkeypatch):
    mongo = SimpleNamespace(
        db=AsyncMongoMockClient().recovery,
        _prepare_for_bson=lambda value: value,
    )
    mysql = MySQL(DatabaseError("mysql unavailable"))
    monkeypatch.setenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true")
    monkeypatch.setattr(
        module.api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=mongo),
    )
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(module, "_copy_tasks", set())
    monkeypatch.setattr(
        module,
        "_copy_state",
        {
            "pending": 0,
            "failed": 0,
            "dead_lettered": 0,
            "fallback_failed": 0,
            "replayed": 0,
            "task_failed": 0,
            "last_failure_time": None,
        },
    )
    return mongo, mysql


async def drain():
    while module._copy_tasks:
        await asyncio.gather(*module._copy_tasks, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback",
    [
        NoSuchTableError("trading_state_mysql_dead_letters"),
        DatabaseError("mysql unavailable"),
    ],
)
async def test_double_failure_tasks_are_observed_and_restart_recovers(
    recovery, fallback, caplog
):
    mongo, mysql = recovery
    mysql.fallback_error = fallback
    reports = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: reports.append(context))
    try:
        assert await module.create_position(
            module.TradingDocument(position_id="p1", status="open")
        ) == {
            "position_id": "p1",
            "created": True,
        }
        await drain()
        assert mysql.attempts == module._COPY_RETRIES
        assert module._copy_state["pending"] == 0
        assert module._copy_state["failed"] == 1
        assert module._copy_state["fallback_failed"] == 1
        assert module._copy_state["dead_lettered"] == 0
        assert module._copy_state["task_failed"] == 0
        assert "trading_state_mysql_dead_letter_failed" in caplog.text
        assert "p1" not in caplog.text
        status = await module.mysql_copy_status()
        assert status["durable_pending"] == 1
        repo = TradingStateRepository(mysql, mongo)
        retained = await repo.pending_copies("positions", 10)
        assert retained[0]["status"] == "open"
        assert retained[0]["_mysql_copy_version"] == 1
        assert await module.get_position("p1") == {
            "position_id": "p1",
            "status": "open",
        }
        module._copy_tasks.clear()
        mysql.unavailable = False
        await module._replay_mysql_copies()
        await module._replay_mysql_copies()
        assert await repo.pending_copy_count() == 0
        assert mysql.effects == 1
        assert mysql.rows[("positions", "p1")]["status"] == "open"
        assert not reports
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_every_typed_mutation_keeps_atomic_current_snapshot(
    recovery, monkeypatch
):
    mongo, mysql = recovery
    monkeypatch.setattr(module, "_schedule_mysql_copy", lambda *args: None)
    await module.create_position(
        module.TradingDocument(position_id="p1", status="open", risk_order_id="r1")
    )
    await module.replace_position("p1", module.TradingDocument(status="open"))
    await module.patch_position("p1", module.TradingDocument(exit_price=12))
    assert await module.close_position("p1", module.TradingDocument(exit_price=15)) == {
        "closed": True
    }
    row = await mongo.db.positions.find_one({"position_id": "p1"})
    assert row["_mysql_copy_version"] == 4
    assert row["_mysql_copy_pending"] is True
    assert row["risk_order_id"] == "r1"
    assert row["status"] == "closed"
    assert row["exit_price"] == 15
    await module.create_position(
        module.TradingDocument(
            position_id="p2",
            status="open",
            symbol="BTCUSDT",
            position_side="long",
        )
    )
    assert await module.close_by_side(
        module.TradingDocument(
            symbol="BTCUSDT",
            position_side="long",
            update={"exit_price": 20},
        )
    ) == {"closed": True, "position_id": "p2"}
    row = await mongo.db.positions.find_one({"position_id": "p2"})
    assert row["_mysql_copy_version"] == 2
    assert row["status"] == "closed"
    for amount in (2.0, 3.0):
        written = await module.put_daily_pnl(
            date(2026, 10, 22), module.DailyPnlRequest(daily_pnl=amount)
        )
        assert written["daily_pnl"] == amount
        assert not any(key.startswith("_mysql_copy_") for key in written)
    repo = TradingStateRepository(mysql, mongo)
    assert await repo.pending_copy_count() == 3
    rows = await repo.pending_copies("daily_pnl", 2)
    assert rows[0]["_mysql_copy_version"] == 2
    mysql.unavailable = False
    await module._replay_mysql_copies()
    assert await repo.pending_copy_count() == 0
    assert mysql.rows[("daily_pnl", "2026-10-22")]["daily_pnl"] == 3
    assert mysql.rows[("positions", "p1")]["risk_order_id"] == "r1"
    assert (await module.list_positions(limit=10))["count"] == 2


@pytest.mark.asyncio
async def test_committed_but_unacknowledged_replay_and_newer_write(
    recovery, monkeypatch
):
    mongo, mysql = recovery
    monkeypatch.setattr(module, "_schedule_mysql_copy", lambda *args: None)
    repo = TradingStateRepository(mysql, mongo)
    await repo.create_position({"position_id": "p1", "status": "open"})
    old = (await repo.pending_copies("positions", 1))[0]
    mysql.unavailable = False
    original = TradingStateRepository.acknowledge_copy
    monkeypatch.setattr(
        TradingStateRepository,
        "acknowledge_copy",
        AsyncMock(side_effect=RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await module._replay_mysql_copies()
    assert mysql.effects == 1
    assert await repo.pending_copy_count() == 1
    monkeypatch.setattr(TradingStateRepository, "acknowledge_copy", original)
    await repo.update_position("p1", {"status": "closed"})
    await repo.acknowledge_copy("positions", old)
    assert await repo.pending_copy_count() == 1
    await module._replay_mysql_copies()
    assert module._copy_to_mysql("positions", old)
    assert mysql.rows[("positions", "p1")]["status"] == "closed"
    assert mysql.effects == 2
    assert await repo.pending_copy_count() == 0


@pytest.mark.asyncio
async def test_bounded_scheduler_and_batch_do_not_block_event_loop(
    recovery, monkeypatch
):
    mongo, mysql = recovery
    repo = TradingStateRepository(mysql, mongo)
    for number in range(30):
        await repo.create_position({"position_id": str(number), "status": "open"})
    entered = asyncio.Event()
    release = asyncio.Event()
    original = module._replay_mysql_copies

    async def paused():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(module, "_replay_mysql_copies", paused)
    for _ in range(100):
        module._schedule_mysql_copy()
    await entered.wait()
    assert len(module._copy_tasks) == 1
    assert module._copy_state["pending"] == 1
    assert (await module.get_position("0"))["status"] == "open"
    release.set()
    await drain()
    assert mysql.attempts == module._COPY_BATCH_SIZE * module._COPY_RETRIES
    assert await repo.pending_copy_count() == 30
    mysql.unavailable = False
    monkeypatch.setattr(module, "_replay_mysql_copies", original)
    await original()
    assert await repo.pending_copy_count() == 5
    await original()
    assert await repo.pending_copy_count() == 0


@pytest.mark.asyncio
async def test_lifespan_worker_resumes_without_new_requests(recovery, monkeypatch):
    mongo, mysql = recovery
    await TradingStateRepository(mysql, mongo).put_daily_pnl(
        "2026-10-22", {"daily_pnl": 2}
    )
    mysql.unavailable = False
    finished = asyncio.Event()
    original = module._replay_mysql_copies

    async def replay():
        await original()
        finished.set()

    monkeypatch.setattr(module, "_replay_mysql_copies", replay)
    async with module.api_module.lifespan(None):
        await asyncio.wait_for(finished.wait(), timeout=2)
    assert not module._copy_tasks
    assert mysql.rows[("daily_pnl", "2026-10-22")]["daily_pnl"] == 2


@pytest.mark.asyncio
async def test_unexpected_task_error_is_retrieved(recovery, monkeypatch, caplog):
    async def fail():
        raise RuntimeError("must not be logged")

    monkeypatch.setattr(module, "_replay_mysql_copies", fail)
    module._schedule_mysql_copy()
    await drain()
    assert module._copy_state["task_failed"] == 1
    assert module._copy_state["pending"] == 0
    assert "trading_state_mysql_replay_task_failed" in caplog.text
    assert "must not be logged" not in caplog.text


def test_client_cannot_supply_copy_metadata():
    with pytest.raises(
        ValidationError, match="reserved trading state metadata"
    ) as error:
        module.TradingDocument(position_id="p1", _mysql_copy_version=999)
    assert error.value.errors()[0]["type"] == "value_error"


@pytest.mark.asyncio
async def test_sql_thread_stall_does_not_block_mongo_requests(recovery, monkeypatch):
    mongo, mysql = recovery
    await TradingStateRepository(mysql, mongo).create_position(
        {"position_id": "p1", "status": "open"}
    )
    mysql.unavailable = False
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    original = mysql.persist_trading_state

    def stalled(*args):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=3)
        return original(*args)

    monkeypatch.setattr(mysql, "persist_trading_state", stalled)
    module._schedule_mysql_copy()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await asyncio.wait_for(module.get_position("p1"), timeout=1) == {
            "position_id": "p1",
            "status": "open",
        }
    finally:
        release.set()
        await drain()
    assert mysql.effects == 1


@pytest.mark.asyncio
async def test_missing_mysql_keeps_routes_available(recovery):
    module.api_module.db_manager.mysql_adapter = None
    assert (await module.create_position(module.TradingDocument(position_id="p1")))[
        "created"
    ]
    await drain()
    assert (await module.get_position("p1"))["position_id"] == "p1"
    assert (await module.mysql_copy_status())["durable_pending"] == 1
    assert module._copy_state["dead_lettered"] == 0
    assert module._copy_state["fallback_failed"] == 1


@pytest.mark.asyncio
async def test_pending_scan_indexes_are_additive_without_ttl(recovery):
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    mongo, _ = recovery
    value = object.__new__(MongoDBAdapter)
    value._connected = True
    value.db = mongo.db
    value.lease_db = None
    for table in ("positions", "daily_pnl"):
        await value.ensure_indexes(table)
        await value.ensure_indexes(table)
        info = await mongo.db[table].index_information()
        index = info["trading_state_mysql_pending"]
        assert list(index["key"]) == [
            ("_mysql_copy_pending", 1),
            ("_mysql_copy_attempted_at", 1),
            ("_id", 1),
        ]
        assert not any("expireAfterSeconds" in row for row in info.values())


def test_unconfirmed_fallback_write_is_not_counted(recovery):
    class Rejected:
        def write(self, *args):
            return 0

    module._dead_letter(Rejected(), "positions", {"position_id": "p1"}, "copy_error")
    assert module._copy_state["dead_lettered"] == 0
    assert module._copy_state["fallback_failed"] == 1
