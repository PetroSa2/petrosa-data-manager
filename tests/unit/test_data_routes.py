import asyncio
from types import SimpleNamespace

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes import data


class Report:
    def to_dict(self):
        return {"ready": True, "collections": []}


@pytest.mark.asyncio
async def test_concurrent_candle_readiness_requests_share_backend_evaluation(
    monkeypatch,
):
    calls = 0

    async def evaluate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return Report()

    data._readiness_cache.clear()
    data._readiness_inflight.clear()
    monkeypatch.setattr(data, "evaluate_readiness", evaluate)
    monkeypatch.setattr(
        api_module, "db_manager", SimpleNamespace(mongodb_adapter=object())
    )

    results = await asyncio.gather(
        data._cached_readiness(pairs=["BTCUSDT"], timeframes=["1h"], min_candles=None),
        data._cached_readiness(pairs=["BTCUSDT"], timeframes=["1h"], min_candles=None),
    )

    assert calls == 1
    assert results == [{"ready": True, "collections": []}] * 2


@pytest.mark.asyncio
async def test_candle_readiness_cache_expires(monkeypatch):
    calls = 0

    async def evaluate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Report()

    data._readiness_cache.clear()
    monkeypatch.setattr(data, "evaluate_readiness", evaluate)
    monkeypatch.setattr(data, "_READINESS_CACHE_SECONDS", 0)
    monkeypatch.setattr(
        api_module, "db_manager", SimpleNamespace(mongodb_adapter=object())
    )
    await data._cached_readiness(pairs=None, timeframes=None, min_candles=None)
    await data._cached_readiness(pairs=None, timeframes=None, min_candles=None)

    assert calls == 2
