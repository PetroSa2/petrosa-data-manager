from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance.dedupe_mongo_klines import (
    choose_survivor,
    dedupe_collection,
)


def test_choose_survivor_prefers_exchange_source():
    documents = [
        {
            "_id": "backfill",
            "source": "data-manager-backfill",
            "extracted_at": datetime(2026, 10, 2, tzinfo=UTC),
        },
        {
            "_id": "exchange",
            "source": "binance-futures",
            "extracted_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]

    assert choose_survivor(documents)["_id"] == "exchange"


@pytest.mark.asyncio
async def test_dedupe_dry_run_reports_without_deleting():
    collection = MagicMock()
    collection.aggregate.return_value.to_list = AsyncMock(
        return_value=[
            {
                "_id": {
                    "symbol": "BTCUSDT",
                    "timestamp": datetime(2026, 10, 2, tzinfo=UTC),
                },
                "count": 2,
            }
        ]
    )
    collection.find.return_value.to_list = AsyncMock(
        return_value=[
            {"_id": "one", "source": "binance-futures"},
            {"_id": "two", "source": "data-manager-backfill"},
        ]
    )

    results = await dedupe_collection(collection, "klines_5m")

    assert results[0].duplicate_documents == 1
    assert results[0].deleted == 0
    collection.delete_many.assert_not_called()
