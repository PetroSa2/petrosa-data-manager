from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import data_manager.api.routes.config as config


@pytest.fixture
def manager():
    global_collection = SimpleNamespace(
        find_one=AsyncMock(return_value={"parameters": {"a": 1, "b": 1}})
    )
    symbol_collection = SimpleNamespace(
        distinct=AsyncMock(return_value=["ETHUSDT", "BTCUSDT"]),
        find_one=AsyncMock(
            side_effect=[
                {"parameters": {"b": 2}},
                {"parameters": {"c": 3}},
            ]
        ),
    )
    manager = SimpleNamespace(
        configuration=object(),
        mongodb=SimpleNamespace(
            db=SimpleNamespace(
                strategy_configs_global=global_collection,
                strategy_configs_symbol=symbol_collection,
            )
        ),
    )
    old_manager = config.db_manager
    config.db_manager = manager
    yield manager
    config.db_manager = old_manager


@pytest.mark.asyncio
async def test_symbols_returns_sorted_distinct_values(manager):
    result = await config.list_strategy_symbols("s1")

    assert result == {"strategy_id": "s1", "symbols": ["BTCUSDT", "ETHUSDT"]}


@pytest.mark.asyncio
async def test_effective_merges_global_symbol_and_side(manager):
    result = await config.get_effective_strategy_config("s1", "BTCUSDT", "LONG")

    assert result == {
        "parameters": {"a": 1, "b": 2, "c": 3},
        "sources": ["global", "symbol", "symbol_side"],
    }


@pytest.mark.asyncio
async def test_get_rejects_side_without_symbol(manager):
    with pytest.raises(HTTPException) as exc_info:
        await config.get_strategy_config("s1", symbol=None, side="LONG")

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_update_rejects_side_without_symbol(manager):
    request = SimpleNamespace(parameters={"a": 1})

    with pytest.raises(HTTPException) as exc_info:
        await config.update_strategy_config("s1", request, symbol=None, side="LONG")

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_delete_uses_split_global_collection(manager):
    manager.mongodb.db.strategy_configs_global.delete_one = AsyncMock()

    result = await config.delete_strategy_config("s1", symbol=None, side=None)

    assert result["message"] == "Configuration deleted successfully"
    manager.mongodb.db.strategy_configs_global.delete_one.assert_awaited_once_with(
        {"strategy_id": "s1"}
    )
