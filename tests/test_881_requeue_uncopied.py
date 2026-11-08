"""petrosa-data-manager#881: the one-off that queues marker-less Mongo positions for the MySQL copy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from data_manager.db.repositories.trading_state_repository import (
    TradingStateRepository,
)
from data_manager.maintenance import requeue_uncopied_positions as module


@pytest_asyncio.fixture
async def setup(monkeypatch):
    monkeypatch.setenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true")
    db = AsyncMongoMockClient().requeue
    positions = db["positions"]
    # Pre-copy-pipeline documents: no copy markers at all (the 603 in production).
    await positions.insert_many(
        [
            {"position_id": f"old{i}", "status": "closed", "created_at": "2026-10-1"}
            for i in range(5)
        ]
    )
    # Already in MySQL, no markers: not a candidate.
    await positions.insert_one({"position_id": "in_mysql", "status": "closed"})
    # Quarantined: left to the copy loop's release rule.
    await positions.insert_one(
        {
            "position_id": "quarantined",
            "status": "closed",
            "_mysql_copy_quarantined": True,
        }
    )
    # Normal copied position with markers.
    await positions.insert_one(
        {"position_id": "copied", "status": "open", "_mysql_copy_version": 3}
    )
    repo = TradingStateRepository(
        None, SimpleNamespace(db=db, _prepare_for_bson=lambda value: value)
    )
    return db, repo


@pytest.mark.asyncio
async def test_dry_run_counts_only_marker_less_positions_missing_from_mysql(setup):
    db, _ = setup

    rows = await module.candidates(db, {"in_mysql"})

    assert sorted(row["position_id"] for row in rows) == [f"old{i}" for i in range(5)]
    assert module.summary(rows) == {
        "candidates": 5,
        "by_status": {"closed": 5},
        "by_created_month": {"2026-10": 5},
    }
    # A dry run writes nothing.
    assert await db["positions"].count_documents({"_mysql_copy_pending": True}) == 0


@pytest.mark.asyncio
async def test_apply_queues_in_batches_through_the_versioned_write(setup, monkeypatch):
    db, repo = setup
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    rows = await module.candidates(db, {"in_mysql"})

    result = await module.requeue(repo, rows, batch=2, pause_seconds=7)

    assert result == {"queued": 5, "skipped": 0}
    assert pauses == [7, 7]  # 3 batches: 2 + 2 + 1
    for i in range(5):
        row = await db["positions"].find_one({"position_id": f"old{i}"})
        assert row["_mysql_copy_pending"] is True
        assert row["_mysql_copy_version"] >= 1
        assert row["status"] == "closed"  # no field changed
    # The worker now finds them, oldest first.
    pending = await repo.pending_copies("positions", 25)
    assert [row["position_id"] for row in pending] == [f"old{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_rerun_finds_nothing_left(setup, monkeypatch):
    db, repo = setup

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    await module.requeue(
        repo, await module.candidates(db, {"in_mysql"}), batch=100, pause_seconds=0
    )

    assert await module.candidates(db, {"in_mysql"}) == []


def test_batch_must_be_positive():
    with pytest.raises(SystemExit) as err:
        module.main(["--batch", "0"])
    assert err.value.code == 2
