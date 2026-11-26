"""petrosa-data-manager#918: scorecard fills come from a shared, TTL-refreshed process cache."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from data_manager.api.routes import strategy_net_r


@pytest.fixture(autouse=True)
def _fresh_cache():
    strategy_net_r._reset_fill_cache()
    yield
    strategy_net_r._reset_fill_cache()


T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _fill(order_id: str, when: datetime) -> dict:
    return {
        "order_id": order_id,
        "event_type": "filled",
        "timestamp": when.replace(tzinfo=None),
    }


def _manager():
    return SimpleNamespace(
        mysql_adapter=SimpleNamespace(query_range=lambda *a, **k: [])
    )


@pytest.mark.asyncio
async def test_a_request_that_times_out_still_fills_the_cache_for_the_next():
    """Review of #919: the first full read must not be lost when the request budget expires."""
    calls: list[int] = []
    release = threading.Event()

    def slow_read(mysql, **kwargs):
        calls.append(1)
        release.wait(2)
        return [_fill("a", T0)], None

    with (
        patch(
            "data_manager.services.historic_analytics.read_execution_events", slow_read
        ),
        patch.object(strategy_net_r.api_module, "db_manager", _manager()),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(strategy_net_r._load_fills(None), 0.05)
        with pytest.raises(
            asyncio.TimeoutError
        ):  # concurrent/next request joins the same read
            await asyncio.wait_for(strategy_net_r._load_fills(None), 0.05)
        release.set()
        rows = await asyncio.wait_for(strategy_net_r._load_fills(None), 2)

    assert [r["order_id"] for r in rows] == ["a"]
    assert len(calls) == 1  # one shared read, not one per request


@pytest.mark.asyncio
async def test_stale_cache_is_served_while_one_refresh_runs(monkeypatch):
    reads = [[_fill("a", T0)], [_fill("a", T0), _fill("b", T0 + timedelta(minutes=1))]]

    def read(mysql, **kwargs):
        return reads.pop(0), None

    monkeypatch.setattr(strategy_net_r, "_FILL_CACHE_TTL_SECONDS", 0.0)
    with (
        patch("data_manager.services.historic_analytics.read_execution_events", read),
        patch.object(strategy_net_r.api_module, "db_manager", _manager()),
    ):
        first = await strategy_net_r._load_fills(None)
        stale = await strategy_net_r._load_fills(
            None
        )  # served at once, refresh started
        await strategy_net_r._fill_task
        fresh = await strategy_net_r._load_fills(None)

    assert [r["order_id"] for r in first] == ["a"]
    assert [r["order_id"] for r in stale] == ["a"]
    assert [r["order_id"] for r in fresh] == ["a", "b"]


@pytest.mark.asyncio
async def test_fresh_cache_does_not_reread_and_end_is_applied_in_memory():
    calls: list[int] = []

    def read(mysql, **kwargs):
        calls.append(1)
        return [_fill("a", T0), _fill("b", T0 + timedelta(hours=2))], None

    with (
        patch("data_manager.services.historic_analytics.read_execution_events", read),
        patch.object(strategy_net_r.api_module, "db_manager", _manager()),
    ):
        rows = await strategy_net_r._load_fills(T0 + timedelta(hours=1))
        again = await strategy_net_r._load_fills(None)
    assert [r["order_id"] for r in rows] == ["a"]
    assert [r["order_id"] for r in again] == ["a", "b"]
    assert len(calls) == 1
    assert time.monotonic() - strategy_net_r._fill_loaded_at < 5
