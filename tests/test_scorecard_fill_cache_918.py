"""petrosa-data-manager#918: scorecard fills come from an incremental process cache."""

from __future__ import annotations

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


def _fill(order_id: str, when: datetime, **extra) -> dict:
    return {
        "order_id": order_id,
        "event_type": "filled",
        "timestamp": when.replace(tzinfo=None),
        "strategy_id": "s",
        "payload": {"position_side": "LONG"},
        **extra,
    }


T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_first_call_reads_all_then_only_new_rows():
    calls: list[dict] = []
    responses = [
        [_fill("a", T0), _fill("b", T0 + timedelta(minutes=10))],
        [
            _fill("b", T0 + timedelta(minutes=10)),
            _fill("c", T0 + timedelta(minutes=20), event_key="x"),
        ],
    ]

    def read(mysql, **kwargs):
        calls.append(kwargs)
        return responses[len(calls) - 1], None

    manager = SimpleNamespace(
        mysql_adapter=SimpleNamespace(query_range=lambda *a, **k: [])
    )
    with (
        patch("data_manager.services.historic_analytics.read_execution_events", read),
        patch.object(strategy_net_r.api_module, "db_manager", manager),
    ):
        first = await strategy_net_r._load_fills(None)
        second = await strategy_net_r._load_fills(None)

    assert calls[0] == {}  # the full read, no bounds
    assert calls[1]["start"] == T0 + timedelta(minutes=10) - timedelta(minutes=5)
    assert [r["order_id"] for r in first] == ["a", "b"]
    assert [r["order_id"] for r in second] == ["a", "b", "c"]  # b once, c added
    assert "event_key" not in second[-1]  # one row shape: the analytics columns


@pytest.mark.asyncio
async def test_end_bound_is_applied_in_memory():
    def read(mysql, **kwargs):
        return [_fill("a", T0), _fill("b", T0 + timedelta(hours=2))], None

    manager = SimpleNamespace(
        mysql_adapter=SimpleNamespace(query_range=lambda *a, **k: [])
    )
    with (
        patch("data_manager.services.historic_analytics.read_execution_events", read),
        patch.object(strategy_net_r.api_module, "db_manager", manager),
    ):
        rows = await strategy_net_r._load_fills(T0 + timedelta(hours=1))
    assert [r["order_id"] for r in rows] == ["a"]
