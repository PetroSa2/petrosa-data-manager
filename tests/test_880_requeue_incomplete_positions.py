from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

from data_manager.db.repositories.trading_state_repository import TradingStateRepository
from data_manager.maintenance import requeue_incomplete_positions as module


@pytest.mark.asyncio
async def test_candidates_only_requeues_non_null_values_missing_in_mysql():
    db = AsyncMongoMockClient().test
    await db.positions.insert_many(
        [
            {"position_id": "fee", "fee_status": "known"},
            {"position_id": "decision", "decision_id": "d1"},
            {"position_id": "complete", "fee_status": "known", "decision_id": "d1"},
            {"position_id": "null", "fee_status": None, "decision_id": None},
        ]
    )
    rows = await module.candidates(
        db,
        [
            {"position_id": "fee", "fee_status": None, "decision_id": None},
            {"position_id": "decision", "fee_status": "known", "decision_id": None},
            {"position_id": "complete", "fee_status": "known", "decision_id": "d1"},
            {"position_id": "null", "fee_status": None, "decision_id": None},
        ],
    )
    assert [row["position_id"] for row in rows] == ["fee", "decision"]


@pytest.mark.asyncio
async def test_requeue_uses_bounded_versioned_empty_updates(monkeypatch):
    db = AsyncMongoMockClient().test
    await db.positions.insert_many([{"position_id": f"p{i}"} for i in range(3)])
    repo = TradingStateRepository(
        None, SimpleNamespace(db=db, _prepare_for_bson=lambda value: value)
    )
    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    result = await module.requeue(
        repo, [{"position_id": f"p{i}"} for i in range(3)], batch=2, pause_seconds=7
    )
    assert result == {"queued": 3, "skipped": 0}
    assert pauses == [7]
    assert await db.positions.count_documents({"_mysql_copy_pending": True}) == 3


def test_mysql_rows_reads_id_and_the_two_fields_then_disconnects(monkeypatch):
    calls = {}

    class FakeMySQL:
        def __init__(self, uri, role):
            calls["role"] = role

        def connect(self):
            calls["connected"] = True

        def find_paginated(self, table, **kwargs):
            calls["query"] = (table, kwargs["columns"], kwargs["include_total"])
            return [{"position_id": "a", "fee_status": None, "decision_id": None}], None

        def disconnect(self):
            calls["disconnected"] = True

    import data_manager.db.mysql_adapter as mysql_module

    monkeypatch.setattr(mysql_module, "MySQLAdapter", FakeMySQL)

    rows = module._mysql_rows()
    assert rows == [{"position_id": "a", "fee_status": None, "decision_id": None}]
    assert calls == {
        "role": "adhoc",
        "connected": True,
        "query": ("positions", ["position_id", "fee_status", "decision_id"], False),
        "disconnected": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
async def test_run_wires_mongo_and_mysql(monkeypatch, apply):
    db = AsyncMongoMockClient().run_test
    await db.positions.insert_many(
        [
            {"position_id": "missing", "fee_status": "known", "decision_id": "d1"},
            {"position_id": "complete", "fee_status": "known", "decision_id": "d1"},
        ]
    )
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
    monkeypatch.setattr(
        module,
        "_mysql_rows",
        lambda: [
            {"position_id": "missing", "fee_status": None, "decision_id": None},
            {"position_id": "complete", "fee_status": "known", "decision_id": "d1"},
        ],
    )

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    result = await module.run(apply=apply, batch=100, pause_seconds=0)

    assert result["candidates"] == 1 and result["apply"] is apply
    assert state["disconnected"] is True
    pending = await db.positions.count_documents({"_mysql_copy_pending": True})
    if apply:
        assert (result["queued"], pending) == (1, 1)
    else:
        assert "queued" not in result and pending == 0


def test_main_prints_the_dry_run_result(monkeypatch, capsys):
    seen = {}

    async def fake_run(*, apply, batch, pause_seconds):
        seen.update(apply=apply, batch=batch, pause=pause_seconds)
        return {"apply": apply, "candidates": 885}

    monkeypatch.setattr(module, "run", fake_run)

    assert module.main([]) == 0
    assert seen == {"apply": False, "batch": 100, "pause": 60.0}
    assert '"candidates": 885' in capsys.readouterr().out


def test_batch_must_be_positive():
    with pytest.raises(SystemExit) as err:
        module.main(["--batch", "0"])
    assert err.value.code == 2
