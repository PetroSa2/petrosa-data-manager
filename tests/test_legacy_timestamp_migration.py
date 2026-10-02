from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance.legacy_timestamp_migration import migrate_collection


def _cursor(rows):
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(side_effect=[rows, []])
    return cursor


@pytest.mark.asyncio
async def test_dry_run_reports_candidates_without_writes():
    collection = MagicMock()
    collection.find.return_value = _cursor(
        [
            {"_id": 1, "timestamp": "2026-10-01T00:00:00Z"},
            {"_id": 2, "timestamp": "not-a-date"},
        ]
    )
    collection.update_one = AsyncMock()

    result = await migrate_collection(
        collection, "klines_5m", batch_size=10, dry_run=True
    )

    assert result.scanned == 2
    assert result.converted == 1
    assert result.invalid == 1
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_converts_valid_rows_in_a_bounded_batch():
    collection = MagicMock()
    collection.find.return_value = _cursor(
        [{"_id": 1, "timestamp": "2026-10-01T00:00:00+02:00"}]
    )
    collection.update_one = AsyncMock()

    result = await migrate_collection(
        collection, "trades_BTCUSDT", batch_size=1, dry_run=False, max_batches=1
    )

    assert result.complete is True
    update = collection.update_one.await_args
    assert update.args[0] == {"_id": 1, "timestamp": "2026-10-01T00:00:00+02:00"}
    converted = update.args[1]["$set"]["timestamp"]
    assert converted.isoformat() == "2026-09-30T22:00:00+00:00"
