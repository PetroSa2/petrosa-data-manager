from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes import data


@pytest.mark.asyncio
async def test_funding_route_pushes_pagination_to_repository(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    repository = SimpleNamespace(
        find_paginated=AsyncMock(
            return_value=([{"timestamp": start, "funding_rate": "0.001"}], 3)
        )
    )
    monkeypatch.setattr(data, "FundingRepository", lambda mysql, mongodb: repository)
    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=None, mongodb_adapter=object()),
    )

    result = await data.get_funding(
        pair="BTCUSDT",
        start=start,
        end=end,
        limit=1,
        offset=2,
        sort_order="desc",
    )

    repository.find_paginated.assert_awaited_once_with(
        "BTCUSDT", start, end, 1, 2, True
    )
    assert result["pagination"]["total"] == 3
    assert result["pagination"]["offset"] == 2
    assert result["data"][0]["funding_rate"] == "0.001"


@pytest.mark.asyncio
async def test_trades_route_pushes_bounded_page_and_count(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    repository = SimpleNamespace(
        get_range=AsyncMock(
            return_value=[
                {
                    "timestamp": start,
                    "trade_id": 1,
                    "price": 10,
                    "quantity": 2,
                    "side": "BUY",
                },
                {
                    "timestamp": end,
                    "trade_id": 2,
                    "price": 11,
                    "quantity": 3,
                    "side": "SELL",
                },
            ]
        ),
        count=AsyncMock(return_value=4),
    )
    monkeypatch.setattr(data, "TradeRepository", lambda mysql, mongodb: repository)
    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=None, mongodb_adapter=object()),
    )

    result = await data.get_trades(
        pair="BTCUSDT",
        start=start,
        end=end,
        limit=1,
        offset=1,
        sort_order="desc",
    )

    repository.get_range.assert_awaited_once_with(
        "BTCUSDT", start, end, limit=2, offset=1, descending=True
    )
    repository.count.assert_awaited_once_with("BTCUSDT", start, end)
    assert result["pagination"]["total"] == 4
    assert result["pagination"]["has_next"] is True
    assert len(result["data"]) == 1
