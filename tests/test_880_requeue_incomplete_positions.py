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
