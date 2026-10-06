import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from data_manager.db.write_result import WriteResult
from data_manager.maintenance.copy_mongo_klines_to_mysql import (
    copy_interval,
    map_documents,
)


def _doc(timestamp):
    return {
        "symbol": "BTCUSDT",
        "timestamp": timestamp,
        "open_price": "1",
        "high_price": "2",
        "low_price": "1",
        "close_price": "2",
        "volume": "3",
    }


def test_dry_run_does_not_write_mysql():
    mongo = SimpleNamespace(
        query_range=AsyncMock(return_value=[_doc(datetime(2026, 1, 1, tzinfo=UTC))])
    )
    mysql = Mock()
    count = asyncio.run(
        copy_interval(
            mongo,
            mysql,
            "15m",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            1,
            False,
        )
    )
    assert count == 1
    mysql.write_batch.assert_not_called()


def test_apply_batches_rows():
    mongo = SimpleNamespace(
        query_range=AsyncMock(
            return_value=[
                _doc(datetime(2026, 1, 1, tzinfo=UTC)),
                _doc(datetime(2026, 1, 1, 1, tzinfo=UTC)),
            ]
        )
    )
    mysql = Mock()
    mysql.write_batch.return_value = WriteResult(inserted=1)
    count = asyncio.run(
        copy_interval(
            mongo,
            mysql,
            "15m",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            1,
            True,
        )
    )
    assert count == 2
    assert mysql.write_batch.call_count == 2


def test_map_documents_skips_invalid_rows():
    assert map_documents([_doc(datetime(2026, 1, 1, tzinfo=UTC))], "15m")
    assert map_documents([{"symbol": "BTCUSDT", "timestamp": "bad"}], "15m") == []
    assert (
        map_documents(
            [
                {
                    "symbol": "BTCUSDT",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open_price": "0",
                    "high_price": "1",
                    "low_price": "1",
                    "close_price": "1",
                }
            ],
            "15m",
        )
        == []
    )
