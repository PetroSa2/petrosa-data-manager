import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis
from data_manager.db.repositories.ledger_repository import LedgerRepository


class _Cursor:
    def __init__(self, rows, owner=None):
        self.rows = rows
        self.owner = owner

    def sort(self, field, direction):
        if self.owner is not None:
            self.owner.sort_calls.append((field, direction))
        self.rows = sorted(
            self.rows, key=lambda row: row.get(field), reverse=direction < 0
        )
        return self

    async def to_list(self, length=None):
        if self.owner is not None:
            self.owner.to_list_lengths.append(length)
        return self.rows[:length] if length is not None else self.rows


class _Mongo:
    def __init__(self, fills):
        self.db = {"execution_events": self}
        self.fills = fills
        self.find_calls = 0
        self.find_filters = []
        self.sort_calls = []
        self.to_list_lengths = []

    def __getitem__(self, _name):
        return self

    def find(self, *_args, **_kwargs):
        self.find_calls += 1
        self.find_filters.append((_args, _kwargs))
        return _Cursor(self.fills, self)


@pytest.fixture
def replay_environment(monkeypatch):
    fills = [{"_id": "fill-1", "timestamp": 1, "event_type": "filled"}]
    history = {"cut_over_ms": 1, "snapshots": [{"as_of_ms": 1, "rows": []}]}
    manager = SimpleNamespace(
        mysql_adapter=object(), mongodb_adapter=SimpleNamespace(db=_Mongo(fills))
    )
    calls = []
    history_reads = []

    class Repository:
        def __init__(self, *_args):
            pass

        def round_exchange_history(self):
            history_reads.append(1)
            return history

        def round_exchange_history_version(self):
            snapshots = history["snapshots"]
            return (
                history["cut_over_ms"],
                max((row["as_of_ms"] for row in snapshots), default=None),
                len(snapshots),
            )

    def build(rows, exchange_history):
        calls.append(threading.get_ident())
        return [
            {"strategy_id": "alpha", "event_type": "filled"},
            {"strategy_id": "beta", "event_type": "filled"},
        ]

    monkeypatch.setattr(
        "data_manager.db.repositories.ledger_repository.LedgerRepository",
        Repository,
    )
    monkeypatch.setattr("data_manager.services.round_book.build_synthetic_exits", build)
    monkeypatch.setattr(api_module, "db_manager", manager)
    analysis._SYNTHETIC_EXITS_CACHE = None
    analysis._SYNTHETIC_EXITS_IN_FLIGHT.clear()
    analysis._EXECUTION_EVENTS_CACHE = None
    analysis._EXCHANGE_HISTORY_CACHE = None
    analysis._LEG_REPLAY_CACHE.clear()
    analysis._BASE_PERFORMANCE_CACHE = None
    analysis._BASE_PERFORMANCE_IN_FLIGHT.clear()
    yield history, fills, calls, history_reads
    analysis._SYNTHETIC_EXITS_CACHE = None
    analysis._SYNTHETIC_EXITS_IN_FLIGHT.clear()
    analysis._EXECUTION_EVENTS_CACHE = None
    analysis._EXCHANGE_HISTORY_CACHE = None
    analysis._LEG_REPLAY_CACHE.clear()
    analysis._BASE_PERFORMANCE_CACHE = None
    analysis._BASE_PERFORMANCE_IN_FLIGHT.clear()
    api_module.db_manager = None


@pytest.mark.asyncio
async def test_replay_cache_filters_strategies_and_runs_off_loop(replay_environment):
    history, fills, calls, _history_reads = replay_environment
    event_loop_thread = threading.get_ident()

    alpha, _ = await analysis._load_synthetic_exits("alpha")
    beta, _ = await analysis._load_synthetic_exits("beta")

    assert [row["strategy_id"] for row in alpha] == ["alpha"]
    assert [row["strategy_id"] for row in beta] == ["beta"]
    assert len(calls) == 1
    assert calls[0] != event_loop_thread

    history["snapshots"].append({"as_of_ms": 2, "rows": []})
    await analysis._load_synthetic_exits("alpha")
    assert len(calls) == 2

    fills.append({"_id": "fill-2", "timestamp": 2, "event_type": "filled"})
    await analysis._load_synthetic_exits("alpha")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_execution_events_are_read_once_per_version(replay_environment):
    _history, fills, _calls, _history_reads = replay_environment
    fills.append({"_id": "fill-2", "timestamp": 2, "event_type": "filled"})
    mongodb = api_module.db_manager.mongodb_adapter

    rows, _latest_fill_id = await analysis._load_execution_events(mongodb)

    assert mongodb.db["execution_events"].find_calls == 1
    assert [row["_id"] for row in rows] == ["fill-1", "fill-2"]
    assert mongodb.db["execution_events"].sort_calls == [("timestamp", -1)]
    assert mongodb.db["execution_events"].to_list_lengths == [analysis._ROUND_MAX_FILLS]


@pytest.mark.asyncio
async def test_execution_events_read_is_capped_and_restores_chronological_order(
    replay_environment, monkeypatch
):
    _history, _fills, _calls, _history_reads = replay_environment
    rows = [
        {"_id": f"fill-{index}", "timestamp": index, "event_type": "filled"}
        for index in range(2048)
    ]
    mongodb = _Mongo(rows)
    monkeypatch.setattr(analysis, "_ROUND_MAX_FILLS", 1024)

    result, latest_id = await analysis._load_execution_events(
        SimpleNamespace(db={"execution_events": mongodb})
    )

    assert len(result) == 1024
    assert [row["timestamp"] for row in result] == list(range(1024, 2048))
    assert latest_id == "fill-2047"
    assert mongodb.to_list_lengths == [1024]


@pytest.mark.asyncio
async def test_execution_events_timeout_is_bounded_and_structured(
    replay_environment, monkeypatch, caplog
):
    _history, fills, _calls, _history_reads = replay_environment
    mongodb = _Mongo(fills)

    async def slow_to_list(length=None):
        await asyncio.sleep(0.05)
        return mongodb.fills[:length]

    cursor = _Cursor(mongodb.fills, mongodb)
    cursor.to_list = slow_to_list
    mongodb.find = lambda *_args, **_kwargs: cursor
    monkeypatch.setattr(
        analysis.constants, "ANALYTICS_DEPENDENCY_TIMEOUT_SECONDS", 0.001
    )

    with caplog.at_level("WARNING", logger=analysis.logger.name):
        with pytest.raises(
            analysis._AnalyticsDependencyTimeout, match="read timed out"
        ):
            await analysis._load_execution_events(
                SimpleNamespace(db={"execution_events": mongodb})
            )

    assert "stage=execution_events_read" in caplog.text
    assert "outcome=timeout" in caplog.text
    assert "limit=50000" in caplog.text


@pytest.mark.asyncio
async def test_legacy_repository_without_version_probe_still_replays(
    replay_environment, monkeypatch
):
    history, _fills, calls, _history_reads = replay_environment

    class LegacyRepository:
        def __init__(self, *_args):
            pass

        def round_exchange_history(self):
            return history

    monkeypatch.setattr(
        "data_manager.db.repositories.ledger_repository.LedgerRepository",
        LegacyRepository,
    )

    rows, cut_over_ms = await analysis._load_synthetic_exits("alpha")

    assert rows == [{"strategy_id": "alpha", "event_type": "filled"}]
    assert cut_over_ms == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_warm_request_does_not_replay_again(replay_environment, monkeypatch):
    _history, _fills, _calls, history_reads = replay_environment
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    replay = AsyncMock(return_value={"outcomes": [], "realized": 0, "complete": False})
    monkeypatch.setattr(analysis, "_timed_leg_replay", replay)

    await analysis.get_strategy_performance("alpha")
    await analysis.get_strategy_performance("alpha")

    assert replay.await_count == 1
    assert len(history_reads) == 1


@pytest.mark.asyncio
async def test_concurrent_requests_share_in_flight_replay(
    replay_environment, monkeypatch
):
    _history, _fills, calls, _history_reads = replay_environment
    started = threading.Event()
    release = threading.Event()

    def slow_build(rows, exchange_history):
        calls.append(threading.get_ident())
        started.set()
        release.wait(timeout=2)
        return [{"strategy_id": "alpha"}]

    monkeypatch.setattr(
        "data_manager.services.round_book.build_synthetic_exits", slow_build
    )
    first = asyncio.create_task(analysis._load_synthetic_exits("alpha"))
    second = asyncio.create_task(analysis._load_synthetic_exits("alpha"))
    await asyncio.to_thread(started.wait, 1)
    assert len(calls) == 1
    release.set()

    await asyncio.gather(first, second)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_repeated_performance_requests_reuse_leg_replay(
    replay_environment, monkeypatch
):
    _history, _fills, _calls, _history_reads = replay_environment
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "apply")
    monkeypatch.setattr(
        analysis, "_load_synthetic_exits", AsyncMock(return_value=([], None))
    )
    replay = AsyncMock(return_value={"outcomes": [], "realized": 0, "complete": False})
    monkeypatch.setattr(analysis, "_timed_leg_replay", replay)

    first_rows, first_version = await analysis._load_execution_events(
        api_module.db_manager.mongodb_adapter
    )
    second_rows, second_version = await analysis._load_execution_events(
        api_module.db_manager.mongodb_adapter
    )
    first = await analysis.get_strategy_performance("alpha")
    second = await analysis.get_strategy_performance("alpha")

    assert second_rows == first_rows
    assert second_version == first_version
    assert first["stats"] == second["stats"]
    assert {
        key: value for key, value in first["metadata"].items() if key != "calculated_at"
    } == {
        key: value
        for key, value in second["metadata"].items()
        if key != "calculated_at"
    }
    assert replay.await_count == 1


@pytest.mark.asyncio
async def test_base_performance_replay_runs_off_event_loop_and_is_cached(
    replay_environment, monkeypatch
):
    _history, fills, _calls, _history_reads = replay_environment
    event_loop_thread = threading.get_ident()
    threads = []

    def calculate(rows):
        threads.append(threading.get_ident())
        return {"alpha": {"outcome": {}, "breakdown": object()}}

    monkeypatch.setattr(analysis, "_calculate_base_performance", calculate)
    mongodb = api_module.db_manager.mongodb_adapter
    await analysis._base_performance(mongodb, fills, "fill-1")
    await analysis._base_performance(mongodb, fills, "fill-1")

    assert len(threads) == 1
    assert threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_empty_exchange_history_returns_empty_result(replay_environment):
    history, _fills, calls, _history_reads = replay_environment
    history["snapshots"] = []

    rows, cut_over_ms = await analysis._load_synthetic_exits("alpha")

    assert rows == []
    assert cut_over_ms == 1
    assert calls == []


@pytest.mark.asyncio
async def test_failed_replay_is_removed_from_in_flight_cache(
    replay_environment, monkeypatch
):
    def fail_build(rows, exchange_history):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(
        "data_manager.services.round_book.build_synthetic_exits", fail_build
    )

    with pytest.raises(RuntimeError, match="replay failed"):
        await analysis._load_synthetic_exits("alpha")

    assert analysis._SYNTHETIC_EXITS_IN_FLIGHT == {}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"earliest": 1, "latest": 3, "snapshot_count": 2}, (1, 3, 2)),
        ({"earliest": None, "latest": None, "snapshot_count": 0}, (None, None, 0)),
    ],
)
def test_round_exchange_history_version(row, expected):
    class Result:
        def mappings(self):
            return self

        def first(self):
            return row

    repository = object.__new__(LedgerRepository)
    repository._run = lambda _query: Result()

    assert repository.round_exchange_history_version() == expected
