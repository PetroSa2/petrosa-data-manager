from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance.dedupe_mongo_klines import (
    _day,
    _extracted_at,
    _run_cli,
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


def test_invalid_dates_use_safe_reporting_values():
    assert _day("not-a-date") == "unknown"
    assert _extracted_at("not-a-date") < datetime(2000, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_dedupe_apply_deletes_non_survivors():
    collection = MagicMock()
    timestamp = datetime(2026, 10, 2, tzinfo=UTC)
    collection.aggregate.return_value.to_list = AsyncMock(
        return_value=[
            {"_id": {"symbol": "BTCUSDT", "timestamp": timestamp}, "count": 2}
        ]
    )
    collection.find.return_value.to_list = AsyncMock(
        return_value=[
            {
                "_id": "old",
                "source": "data-manager-backfill",
                "extracted_at": timestamp,
            },
            {
                "_id": "new",
                "source": "data-manager-backfill",
                "extracted_at": timestamp.replace(day=3),
            },
        ]
    )
    collection.delete_many = AsyncMock(return_value=SimpleNamespace(deleted_count=1))

    results = await dedupe_collection(collection, "klines_5m", dry_run=False)

    assert results[0].deleted == 1
    collection.delete_many.assert_awaited_once_with({"_id": {"$in": ["old"]}})


@pytest.mark.asyncio
async def test_cli_defaults_to_dry_run_and_selects_kline_collections(
    monkeypatch, capsys
):
    collection = MagicMock()
    collection.aggregate.return_value.to_list = AsyncMock(return_value=[])
    adapter = MagicMock()
    adapter.list_collections = AsyncMock(return_value=["klines_5m", "other"])
    adapter.db = {"klines_5m": collection}

    monkeypatch.setattr(
        "data_manager.maintenance.dedupe_mongo_klines.MongoDBAdapter",
        lambda *_args, **_kwargs: adapter,
    )

    await _run_cli(False, None)

    assert capsys.readouterr().out == "[]\n"
    adapter.connect.assert_called_once()
    adapter.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_cli_rejects_unknown_collection(monkeypatch):
    adapter = MagicMock()
    adapter.list_collections = AsyncMock(return_value=["klines_5m"])
    monkeypatch.setattr(
        "data_manager.maintenance.dedupe_mongo_klines.MongoDBAdapter",
        lambda *_args, **_kwargs: adapter,
    )

    with pytest.raises(ValueError, match="collection not found"):
        await _run_cli(False, "klines_1h")

    adapter.disconnect.assert_called_once()
