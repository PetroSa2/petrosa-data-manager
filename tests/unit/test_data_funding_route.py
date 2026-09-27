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
