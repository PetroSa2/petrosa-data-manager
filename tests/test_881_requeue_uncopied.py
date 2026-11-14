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


@pytest.mark.asyncio
async def test_a_position_changed_meanwhile_is_counted_as_skipped(setup, monkeypatch):
    db, repo = setup

    async def unchanged(position_id, data):
        return 0

    monkeypatch.setattr(repo, "update_position", unchanged)

    result = await module.requeue(
        repo, await module.candidates(db, {"in_mysql"}), batch=100, pause_seconds=0
    )

    assert result == {"queued": 0, "skipped": 5}


def test_mysql_position_ids_reads_only_ids_and_disconnects(monkeypatch):
    calls = {}

    class FakeMySQL:
        def __init__(self, uri, role):
            calls["role"] = role

        def connect(self):
            calls["connected"] = True

        def find_paginated(self, table, **kwargs):
            calls["query"] = (table, kwargs["columns"], kwargs["include_total"])
            return [
                {"position_id": "a"},
                {"position_id": ""},
                {"position_id": "b"},
            ], None

        def disconnect(self):
            calls["disconnected"] = True

    import data_manager.db.mysql_adapter as mysql_module

    monkeypatch.setattr(mysql_module, "MySQLAdapter", FakeMySQL)

    assert module._mysql_position_ids() == {"a", "b"}
    assert calls == {
        "role": "adhoc",
        "connected": True,
        "query": ("positions", ["position_id"], False),
        "disconnected": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
async def test_run_wires_mongo_and_mysql(setup, monkeypatch, apply):
    db, _ = setup
    state = {}

    class FakeMongo:
        def __init__(self, url, database_name):
            self.db = db
            state["database"] = database_name

        @staticmethod
        def _prepare_for_bson(value):
            return value

        def connect(self):
            state["connected"] = True

        def disconnect(self):
            state["disconnected"] = True

    import data_manager.db.mongodb_adapter as mongo_module

    monkeypatch.setattr(mongo_module, "MongoDBAdapter", FakeMongo)
    monkeypatch.setattr(module, "_mysql_position_ids", lambda: {"in_mysql"})

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    result = await module.run(apply=apply, batch=100, pause_seconds=0)

    assert result["candidates"] == 5 and result["apply"] is apply
    assert state["disconnected"] is True
    pending = await db["positions"].count_documents({"_mysql_copy_pending": True})
    if apply:
        assert (result["queued"], pending) == (5, 5)
    else:
        assert "queued" not in result and pending == 0


def test_main_prints_the_dry_run_result(monkeypatch, capsys):
    seen = {}

    async def fake_run(*, apply, batch, pause_seconds):
        seen.update(apply=apply, batch=batch, pause=pause_seconds)
        return {"apply": apply, "candidates": 603}

    monkeypatch.setattr(module, "run", fake_run)

    assert module.main([]) == 0
    assert seen == {"apply": False, "batch": 100, "pause": 60.0}
    assert '"candidates": 603' in capsys.readouterr().out
