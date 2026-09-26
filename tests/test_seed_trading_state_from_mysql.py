import pytest

from data_manager.maintenance import seed_trading_state_from_mysql as module


class FakeMongoCollection:
    def __init__(self):
        self.writes = []

    async def update_one(self, query, update, upsert):
        self.writes.append((query, update, upsert))


class FakeMongo:
    def __init__(self):
        self.collection = FakeMongoCollection()
        self.db = {"positions": self.collection, "daily_pnl": self.collection}

    def connect(self):
        return None

    def disconnect(self):
        return None

    @staticmethod
    def _prepare_for_bson(value):
        return value


class FakeMysql:
    def connect(self):
        return None

    def disconnect(self):
        return None

    def find_paginated(self, collection, **kwargs):
        if collection == "positions":
            return ([{"position_id": "p1", "status": "open"}], 1)
        return ([{"date": "2026-09-26", "daily_pnl": 2.0}], 1)


@pytest.mark.asyncio
async def test_seed_dry_run_does_not_write(monkeypatch):
    mongo = FakeMongo()
    monkeypatch.setattr(module, "MongoDBAdapter", lambda *args, **kwargs: mongo)
    monkeypatch.setattr(module, "MySQLAdapter", lambda *args, **kwargs: FakeMysql())
    result = await module.seed(apply=False)
    assert result == {"positions": 2, "daily_pnl": 1}
    assert mongo.collection.writes == []


@pytest.mark.asyncio
async def test_seed_apply_uses_set_on_insert(monkeypatch):
    mongo = FakeMongo()
    monkeypatch.setattr(module, "MongoDBAdapter", lambda *args, **kwargs: mongo)
    monkeypatch.setattr(module, "MySQLAdapter", lambda *args, **kwargs: FakeMysql())
    await module.seed(apply=True)
    assert len(mongo.collection.writes) == 3
    assert all(
        "$setOnInsert" in update and upsert
        for _, update, upsert in mongo.collection.writes
    )
