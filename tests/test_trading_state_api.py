from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from data_manager.api.routes import trading_state as module


class FakeRepo:
    def __init__(self):
        self.position = {"position_id": "p1", "status": "open"}

    async def create_position(self, document):
        assert document["position_id"] == "p1"

    async def get_position(self, position_id):
        return self.position if position_id == "p1" else None

    async def update_position(self, position_id, data, *, upsert=False):
        self.position.update(data)
        return 1

    async def close_position(self, position_id, data):
        self.position.update(data, status="closed")
        return position_id == "p1"

    async def close_by_side(self, symbol, side, data):
        return "p1"

    async def list_positions(self, filters, limit):
        return [self.position]

    async def get_daily_pnl(self, value):
        return {"date": value, "daily_pnl": 1.0}

    async def put_daily_pnl(self, value, data):
        return data


@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(module, "_repo", lambda: repo)
    monkeypatch.setattr(module, "_schedule_mysql_copy", lambda *args: None)
    return repo


@pytest.mark.asyncio
async def test_create_duplicate_and_required_id(fake_repo):
    result = await module.create_position(module.TradingDocument(position_id="p1"))
    assert result["created"] is True
    with pytest.raises(HTTPException) as error:
        await module.create_position(module.TradingDocument())
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_put_reopen_is_rejected(fake_repo):
    fake_repo.position["status"] = "closed"
    with pytest.raises(HTTPException) as error:
        await module.replace_position("p1", module.TradingDocument(status="open"))
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_patch_and_get_missing(fake_repo):
    result = await module.patch_position(
        "p1", module.TradingDocument(risk_order_id="o1")
    )
    assert result["risk_order_id"] == "o1"
    with pytest.raises(HTTPException) as error:
        await module.get_position("missing")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_close_and_close_by_side(fake_repo):
    assert (await module.close_position("p1", module.TradingDocument(exit_price=2)))[
        "closed"
    ]
    result = await module.close_by_side(
        module.TradingDocument(symbol="BTCUSDT", position_side="long", update={})
    )
    assert result == {"closed": True, "position_id": "p1"}


@pytest.mark.asyncio
async def test_list_and_daily_pnl(fake_repo):
    result = await module.list_positions(status="open,partially_closed", limit=2)
    assert result["count"] == 1
    assert (await module.get_daily_pnl(date(2026, 1, 1)))["daily_pnl"] == 1.0
    written = await module.put_daily_pnl(
        date(2026, 1, 1), module.DailyPnlRequest(daily_pnl=2)
    )
    assert written["daily_pnl"] == 2


@pytest.mark.asyncio
async def test_database_failures_are_503(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(module, "_repo", lambda: SimpleNamespace(get_position=fail))
    with pytest.raises(HTTPException) as error:
        await module.get_position("p1")
    assert error.value.status_code == 503


def test_mysql_copy_is_best_effort(monkeypatch):
    class Adapter:
        def get_column_names(self, table):
            return {"position_id", "status"}

        def update(self, *args):
            return 0

        def write(self, rows, table):
            assert table == "positions"
            assert rows

    monkeypatch.setattr(
        module.api_module, "db_manager", SimpleNamespace(mysql_adapter=Adapter())
    )
    module._copy_to_mysql(
        "positions", {"position_id": "p1", "status": "closed", "unknown": 1}
    )
