from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.db.repositories.funding_repository import FundingRepository
from data_manager.models.market_data import FundingRate


def _rate() -> FundingRate:
    return FundingRate(
        symbol="BTCUSDT",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        funding_rate=Decimal("0.001"),
    )


@pytest.mark.asyncio
async def test_funding_insert_writes_mongo_and_mysql():
    mongo = MagicMock()
    mongo.write = AsyncMock(return_value=1)
    mysql = MagicMock()
    repo = FundingRepository(mysql, mongo)
    assert await repo.insert(_rate()) is True
    mongo.write.assert_awaited_once()
    mysql.write.assert_called_once_with([_rate()], "funding_rates")


@pytest.mark.asyncio
async def test_funding_mysql_failure_does_not_block_mongo():
    mongo = MagicMock()
    mongo.write = AsyncMock(return_value=1)
    mysql = MagicMock()
    mysql.write.side_effect = RuntimeError("offline")
    repo = FundingRepository(mysql, mongo)
    assert await repo.insert(_rate()) is True
    mongo.write.assert_awaited_once()
