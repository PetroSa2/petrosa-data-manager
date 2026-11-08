"""petrosa-data-manager#881: quarantined MySQL copies are released again, bounded, instead of staying out forever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

from data_manager.db.repositories import trading_state_repository as repo_module
from data_manager.db.repositories.trading_state_repository import (
    TradingStateRepository,
)


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true")
    mongo = SimpleNamespace(
        db=AsyncMongoMockClient().release, _prepare_for_bson=lambda value: value
    )
    return TradingStateRepository(None, mongo)


async def _quarantine(repo: TradingStateRepository, position_id: str) -> dict:
    """Drive a copy through the real defer path until it is quarantined."""
    collection = repo._collection("positions")
    for _ in range(5):
        row = await collection.find_one({"position_id": position_id})
        await repo.defer_copy("positions", row)
    row = await collection.find_one({"position_id": position_id})
    assert row["_mysql_copy_quarantined"] is True
    return row


@pytest.mark.asyncio
async def test_quarantine_records_its_version_and_time(repo):
    await repo.create_position({"position_id": "p1", "status": "open"})

    row = await _quarantine(repo, "p1")

    assert row["_mysql_copy_quarantined_version"] == row["_mysql_copy_version"]
    assert isinstance(row["_mysql_copy_quarantined_at"], datetime)
    assert await repo.quarantined_count("positions") == 1
    assert await repo.pending_copies("positions", 25) == []


@pytest.mark.asyncio
async def test_unchanged_quarantine_waits_for_the_cooloff(repo):
    await repo.create_position({"position_id": "p1", "status": "open"})
    await _quarantine(repo, "p1")

    assert await repo.release_due_quarantines("positions", 100) == 0
    assert await repo.release_due_quarantines("positions", 100, cooloff_seconds=0) == 1
    assert [r["position_id"] for r in await repo.pending_copies("positions", 25)] == [
        "p1"
    ]


@pytest.mark.asyncio
async def test_a_newer_version_releases_the_quarantine_and_resets_attempts(repo):
    await repo.create_position({"position_id": "p1", "status": "open"})
    await _quarantine(repo, "p1")

    # The 57 reconciler closes: the position changed after it was quarantined.
    await repo.close_position("p1", {"exit_price": 2.0})
    assert await repo.release_due_quarantines("positions", 100) == 1

    row = await repo._collection("positions").find_one({"position_id": "p1"})
    assert row["_mysql_copy_quarantined"] is False
    assert row["_mysql_copy_pending"] is True
    assert row["_mysql_copy_attempts"] == 0
    assert row["_mysql_copy_releases"] == 1
    assert row["status"] == "closed"
    pending = await repo.pending_copies("positions", 25)
    assert [(r["position_id"], r["status"]) for r in pending] == [("p1", "closed")]


@pytest.mark.asyncio
async def test_a_quarantine_from_before_881_is_released(repo):
    """The 540 production documents have no quarantine version or time recorded."""
    await repo.create_position({"position_id": "legacy", "status": "closed"})
    await repo._collection("positions").update_one(
        {"position_id": "legacy"},
        {"$set": {"_mysql_copy_quarantined": True, "_mysql_copy_attempts": 65}},
    )

    assert await repo.release_due_quarantines("positions", 100) == 1
    row = await repo._collection("positions").find_one({"position_id": "legacy"})
    assert (row["_mysql_copy_quarantined"], row["_mysql_copy_attempts"]) == (False, 0)


@pytest.mark.asyncio
async def test_release_is_bounded_per_cycle(repo):
    for n in range(7):
        await repo.create_position({"position_id": f"p{n}", "status": "closed"})
    await repo._collection("positions").update_many(
        {}, {"$set": {"_mysql_copy_quarantined": True}}
    )

    assert await repo.release_due_quarantines("positions", 3) == 3
    assert await repo.quarantined_count("positions") == 4
    assert await repo.release_due_quarantines("positions", 3) == 3
    assert await repo.release_due_quarantines("positions", 3) == 1
    assert await repo.quarantined_count("positions") == 0


@pytest.mark.asyncio
async def test_a_recurring_error_stops_after_max_releases(repo):
    """Never an infinite loop: after the cap the document stays quarantined (and counted on the gauge)."""
    await repo.create_position({"position_id": "poison", "status": "open"})
    for release in range(repo_module.QUARANTINE_MAX_RELEASES):
        await _quarantine(repo, "poison")
        assert (
            await repo.release_due_quarantines("positions", 100, cooloff_seconds=0) == 1
        ), release

    await _quarantine(repo, "poison")
    assert await repo.release_due_quarantines("positions", 100, cooloff_seconds=0) == 0
    assert await repo.quarantined_count("positions") == 1
    row = await repo._collection("positions").find_one({"position_id": "poison"})
    assert row["_mysql_copy_releases"] == repo_module.QUARANTINE_MAX_RELEASES


@pytest.mark.asyncio
async def test_an_old_quarantine_is_due_after_the_cooloff(repo):
    await repo.create_position({"position_id": "p1", "status": "open"})
    await _quarantine(repo, "p1")
    await repo._collection("positions").update_one(
        {"position_id": "p1"},
        {
            "$set": {
                "_mysql_copy_quarantined_at": datetime.now(UTC)
                - timedelta(seconds=repo_module.QUARANTINE_COOLOFF_SECONDS + 1)
            }
        },
    )

    assert await repo.release_due_quarantines("positions", 100) == 1
