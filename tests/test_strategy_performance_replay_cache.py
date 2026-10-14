import asyncio
import threading
from types import SimpleNamespace

import pytest

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, field, direction):
        self.rows = sorted(
            self.rows, key=lambda row: row.get(field), reverse=direction < 0
        )
        return self

    async def to_list(self, length=None):
        return self.rows[:length] if length is not None else self.rows


class _Mongo:
    def __init__(self, fills):
        self.db = {"execution_events": self}
        self.fills = fills

    def __getitem__(self, _name):
        return self

    def find(self, *_args, **_kwargs):
        return _Cursor(self.fills)


@pytest.fixture
def replay_environment(monkeypatch):
    fills = [{"_id": "fill-1", "timestamp": 1, "event_type": "filled"}]
    history = {"cut_over_ms": 1, "snapshots": [{"as_of_ms": 1, "rows": []}]}
    manager = SimpleNamespace(
        mysql_adapter=object(), mongodb_adapter=SimpleNamespace(db=_Mongo(fills))
    )
    calls = []

    class Repository:
        def __init__(self, *_args):
            pass

        def round_exchange_history(self):
            return history

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
    yield history, fills, calls
    analysis._SYNTHETIC_EXITS_CACHE = None
    analysis._SYNTHETIC_EXITS_IN_FLIGHT.clear()
    api_module.db_manager = None


@pytest.mark.asyncio
async def test_replay_cache_filters_strategies_and_runs_off_loop(replay_environment):
    history, fills, calls = replay_environment
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
async def test_concurrent_requests_share_in_flight_replay(
    replay_environment, monkeypatch
):
    _history, _fills, calls = replay_environment
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
async def test_empty_exchange_history_returns_empty_result(replay_environment):
    history, _fills, calls = replay_environment
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
