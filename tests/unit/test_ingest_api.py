import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data_manager.api.routes import ingest


def _manager(collection, mysql=None):
    return SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"klines_15m": collection}),
        mysql_adapter=mysql,
    )


def _kline(price="1"):
    return {
        "timestamp": "2026-09-24T00:00:00Z",
        "open_price": price,
        "high_price": "2",
        "low_price": "1",
        "close_price": "2",
        "volume": "3",
    }


def test_ingest_klines_writes_and_schedules_mysql_copy(monkeypatch):
    collection = Mock()
    collection.bulk_write = AsyncMock(
        return_value=SimpleNamespace(upserted_count=1, matched_count=2)
    )
    mysql = Mock()
    ingest.set_database_manager(_manager(collection, mysql))

    result = asyncio.run(
        ingest.ingest_klines(
            ingest.KlinesRequest(symbol="BTCUSDT", interval="15m", klines=[_kline()])
        )
    )
    assert result["upserted"] == 1
    assert result["matched"] == 2
    assert result["mysql_copy"] == "scheduled"
    assert collection.bulk_write.await_count == 1
    asyncio.run(asyncio.sleep(0))


def test_ingest_klines_rejects_bad_docs_and_honors_kill_switch(monkeypatch):
    collection = Mock()
    collection.bulk_write = AsyncMock(return_value=SimpleNamespace())
    mysql = Mock()
    monkeypatch.setenv("PETROSA_KLINES_MYSQL_COPY_ENABLED", "false")
    ingest.set_database_manager(_manager(collection, mysql))

    result = asyncio.run(
        ingest.ingest_klines(
            ingest.KlinesRequest(symbol="BTCUSDT", interval="15m", klines=[_kline("0")])
        )
    )
    assert result["rejected"] == 1
    assert result["mysql_copy"] == "disabled"
    collection.bulk_write.assert_not_awaited()


def test_ingest_klines_validates_interval_and_size(monkeypatch):
    from fastapi import HTTPException

    ingest.set_database_manager(_manager(Mock()))
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(
            ingest.ingest_klines(
                ingest.KlinesRequest(symbol="BTCUSDT", interval="7m", klines=[])
            )
        )
    assert invalid.value.status_code == 422


def test_ingest_funding_builds_models(monkeypatch):
    collection = Mock()
    ingest.set_database_manager(_manager(collection))
    inserted = AsyncMock(return_value=2)
    monkeypatch.setattr(ingest.FundingRepository, "insert_batch", inserted)

    result = asyncio.run(
        ingest.ingest_funding(
            ingest.FundingRequest(
                symbol="BTCUSDT",
                rates=[
                    {"funding_time": "2026-09-24T00:00:00Z", "funding_rate": "0.1"},
                    {"timestamp": "2026-09-24T08:00:00Z", "funding_rate": 0.2},
                ],
            )
        )
    )
    assert result == {"symbol": "BTCUSDT", "received": 2, "inserted": 2}
    assert inserted.await_count == 1
    assert len(inserted.await_args.args[0]) == 2


def test_ingest_reports_unavailable_mysql_and_invalid_funding():
    collection = Mock()
    collection.bulk_write = AsyncMock(return_value=SimpleNamespace())
    ingest.set_database_manager(_manager(collection, None))
    result = asyncio.run(
        ingest.ingest_klines(
            ingest.KlinesRequest(symbol="BTCUSDT", interval="15m", klines=[])
        )
    )
    assert result["mysql_copy"] == "unavailable"

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as invalid:
        asyncio.run(
            ingest.ingest_funding(
                ingest.FundingRequest(symbol="BTCUSDT", rates=[{"funding_rate": 1}])
            )
        )
    assert invalid.value.status_code == 422


def test_ingest_requires_mongo():
    from fastapi import HTTPException

    ingest.set_database_manager(
        SimpleNamespace(mongodb_adapter=None, mysql_adapter=None)
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(
            ingest.ingest_klines(
                ingest.KlinesRequest(symbol="BTCUSDT", interval="15m", klines=[])
            )
        )
    assert failure.value.status_code == 503
