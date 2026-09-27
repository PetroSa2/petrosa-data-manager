from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import data_manager.api.routes.strategy_lifecycle as routes
from data_manager.db.repositories.strategy_lifecycle_repository import (
    StrategyLifecycleRepository,
)


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sort_args = None
        self.limit_arg = None

    def sort(self, field, direction):
        self.sort_args = (field, direction)
        return self

    def limit(self, value):
        self.limit_arg = value
        return self

    async def to_list(self, length):
        return self.rows[:length]


@pytest.mark.asyncio
async def test_repository_inserts_timezone_aware_event():
    collection = MagicMock()
    collection.insert_one = AsyncMock(
        return_value=SimpleNamespace(inserted_id="event-1")
    )
    adapter = SimpleNamespace(db={"strategy_lifecycle_events": collection})
    repo = StrategyLifecycleRepository(None, adapter)

    event = {
        "strategy_id": "s1",
        "to_state": "running",
        "transitioned_at": datetime.now(UTC),
    }
    saved = await repo.insert_event(event)

    assert saved["event_id"] == "event-1"
    assert (
        collection.insert_one.await_args.args[0]["transitioned_at"].tzinfo is not None
    )


@pytest.mark.asyncio
async def test_repository_state_uses_latest_sort():
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=None)
    repo = StrategyLifecycleRepository(
        None, SimpleNamespace(db={"strategy_lifecycle_events": collection})
    )

    assert await repo.get_state("s1") is None
    collection.find_one.assert_awaited_once_with(
        {"strategy_id": "s1"}, sort=[("transitioned_at", -1)]
    )


@pytest.mark.asyncio
async def test_repository_events_passes_order_and_limit():
    cursor = Cursor([])
    collection = MagicMock()
    collection.find.return_value = cursor
    repo = StrategyLifecycleRepository(
        None, SimpleNamespace(db={"strategy_lifecycle_events": collection})
    )

    assert await repo.get_events("s1", 2, "asc") == []
    assert cursor.sort_args == ("transitioned_at", 1)
    assert cursor.limit_arg == 2


@pytest.mark.asyncio
async def test_routes_create_and_get_state(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repo = MagicMock()
    repo.insert_event = AsyncMock(
        return_value={
            "event_id": "event-1",
            "strategy_id": "s1",
            "to_state": "running",
            "transitioned_at": now,
        }
    )
    repo.get_state = AsyncMock(
        return_value={"_id": "event-1", "to_state": "running", "transitioned_at": now}
    )
    monkeypatch.setattr(routes, "_repo", lambda: repo)

    created = await routes.create_event(
        routes.LifecycleEventRequest(to_state="running", transitioned_by="test"), "s1"
    )
    state = await routes.get_state("s1")

    assert created["event_id"] == "event-1"
    assert created["transitioned_at"].endswith("+00:00")
    assert state["state"] == "running"
    assert repo.insert_event.await_args.args[0]["transitioned_at"].tzinfo is not None
    repo.insert_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_routes_empty_state_and_events(monkeypatch):
    repo = MagicMock()
    repo.get_state = AsyncMock(return_value=None)
    repo.get_events = AsyncMock(return_value=[])
    monkeypatch.setattr(routes, "_repo", lambda: repo)

    assert (await routes.get_state("s1"))["state"] is None
    assert await routes.get_events("s1", 2, "asc") == {
        "strategy_id": "s1",
        "events": [],
        "count": 0,
    }
