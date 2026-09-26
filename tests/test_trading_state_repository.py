from types import SimpleNamespace

import pytest

from data_manager.db.repositories.trading_state_repository import TradingStateRepository


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self, length):
        return list(self.rows)


class Collection:
    def __init__(self):
        self.rows = [
            {
                "position_id": "p1",
                "status": "open",
                "entry_time": 2,
                "symbol": "BTCUSDT",
                "position_side": "long",
            }
        ]

    async def insert_one(self, document):
        self.rows.append(document)

    async def find_one(self, query, sort=None):
        for row in self.rows:
            if all(
                row.get(key) == value or isinstance(value, dict)
                for key, value in query.items()
            ):
                return dict(row)
        return None

    def find(self, query):
        return Cursor(self.rows)

    async def update_one(self, query, update, upsert=False):
        return SimpleNamespace(modified_count=1, upserted_id=None)


class Mongo:
    def __init__(self):
        self.db = {"positions": Collection(), "daily_pnl": Collection()}

    @staticmethod
    def _prepare_for_bson(value):
        return value


@pytest.mark.asyncio
async def test_trading_state_repository_operations():
    repo = TradingStateRepository(None, Mongo())
    assert await repo.create_position({"position_id": "p2"}) is True
    assert (await repo.get_position("p1"))["position_id"] == "p1"
    assert await repo.update_position("p1", {"status": "open"}, upsert=True) == 1
    assert await repo.close_position("p1", {"exit_price": 2}) is True
    assert await repo.close_by_side("BTCUSDT", "long", {}) == "p1"
    assert len(await repo.list_positions({}, 10)) == 2


@pytest.mark.asyncio
async def test_daily_pnl_repository_operations():
    repo = TradingStateRepository(None, Mongo())
    assert await repo.get_daily_pnl("2026-09-26") is None
    result = await repo.put_daily_pnl("2026-09-26", {"daily_pnl": 2.0})
    assert result["daily_pnl"] == 2.0
