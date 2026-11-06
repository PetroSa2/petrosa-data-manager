"""Inputs of the exchange ground truth (dm#844): Mongo rows, MySQL journal, fills."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from data_manager.services.exchange_ground_truth_inputs import (
    GroundTruthInputError,
    compute_ground_truth,
    read_order_journal,
    read_position_rows,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
START = NOW - timedelta(days=7)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.cap = None

    def limit(self, cap):
        self.cap = cap
        return self

    async def to_list(self, length):
        return self.rows[: self.cap]


class _Mongo:
    def __init__(self, rows):
        self.cursor = _Cursor(rows)
        self.db = {"strategy_positions": self}

    def find(self, query, projection):
        assert projection["_id"] == 0
        return self.cursor


class _MySQL:
    def __init__(self, rows):
        self.engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        with self.engine.begin() as c:
            c.execute(
                text(
                    "CREATE TABLE execution_events (order_id TEXT, strategy_id TEXT, "
                    "event_type TEXT, symbol TEXT, payload TEXT, timestamp TEXT)"
                )
            )
            for r in rows:
                c.execute(
                    text(
                        "INSERT INTO execution_events VALUES "
                        "(:order_id, :strategy_id, :event_type, :symbol, :payload, :timestamp)"
                    ),
                    {**r, "payload": json.dumps(r.get("payload", {}))},
                )

    def _ensure_connected(self):
        return self.engine


def _event(order_id, strategy, event_type, days_ago=1, payload=None, symbol="BTCUSDT"):
    ts = (NOW - timedelta(days=days_ago)).replace(tzinfo=None)
    return {
        "order_id": order_id,
        "strategy_id": strategy,
        "event_type": event_type,
        "symbol": symbol,
        "payload": payload or {},
        "timestamp": ts.isoformat(sep=" "),
    }


async def test_position_rows_are_filtered_to_the_window_for_both_time_kinds() -> None:
    rows = [
        {"strategy_position_id": "a", "entry_time": NOW - timedelta(days=1)},
        {
            "strategy_position_id": "b",
            "entry_time": (NOW - timedelta(days=2)).isoformat(),
        },
        {
            "strategy_position_id": "c",
            "entry_time": NOW - timedelta(days=30),
            "exit_time": (NOW - timedelta(days=3)).isoformat(),
        },
        {"strategy_position_id": "d", "entry_time": NOW - timedelta(days=30)},
    ]
    got = await read_position_rows(_Mongo(rows), START, NOW)
    assert [r["strategy_position_id"] for r in got] == ["a", "b", "c"]


async def test_position_read_over_the_cap_is_an_error(monkeypatch) -> None:
    import data_manager.services.exchange_ground_truth_inputs as inputs

    monkeypatch.setattr(inputs, "MAX_POSITION_ROWS", 1)
    rows = [{"entry_time": NOW}, {"entry_time": NOW}]
    with pytest.raises(GroundTruthInputError):
        await read_position_rows(_Mongo(rows), START, NOW)


async def test_journal_maps_orders_exits_and_conflicts() -> None:
    mysql = _MySQL(
        [
            _event("100", "iceberg_detector", "placed"),
            _event("100", "iceberg_detector", "filled"),
            _event(
                "reconcile-BTCUSDT-LONG-x-1",
                "iceberg_detector",
                "position_closed",
                payload={"entry_order_id": "100", "exit_order_id": "200"},
            ),
            _event("300", "spread_liquidity", "filled", symbol="ETHUSDT"),
            _event("300", "doji_reversal", "filled", symbol="ETHUSDT"),
            _event("400", "unknown", "filled"),
            _event("500", "old", "filled", days_ago=30),
        ]
    )
    order_strategy, exits, symbols, conflicts = read_order_journal(mysql, START, NOW)
    assert order_strategy == {"100": "iceberg_detector", "200": "iceberg_detector"}
    assert exits == {"100": {"200"}}
    assert symbols == {"BTCUSDT", "ETHUSDT"}
    assert conflicts == ["300"]


async def test_compute_reads_every_symbol_and_builds_the_report() -> None:
    entry_ms = int((NOW - timedelta(days=1)).timestamp() * 1000)
    rows = [
        {
            "strategy_position_id": "sp1",
            "strategy_id": "iceberg_detector",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "status": "closed",
            "entry_order_id": "100",
            "entry_time": NOW - timedelta(days=1),
        }
    ]
    journal = [
        _event(
            "x",
            "iceberg_detector",
            "position_closed",
            payload={"entry_order_id": "100", "exit_order_id": "200"},
        )
    ]
    client = AsyncMock()
    client.fills.return_value = [
        {
            "id": 1,
            "orderId": 100,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "qty": "1",
            "realizedPnl": "0",
            "commission": "0",
            "time": entry_ms,
        },
        {
            "id": 2,
            "orderId": 200,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "102",
            "qty": "1",
            "realizedPnl": "2",
            "commission": "0",
            "time": entry_ms + 60_000,
        },
    ]
    client.orders.return_value = []
    db = type(
        "Db", (), {"mongodb_adapter": _Mongo(rows), "mysql_adapter": _MySQL(journal)}
    )()

    report = await compute_ground_truth(db, window_days=7, client=client, now=NOW)

    client.fills.assert_awaited_once()
    assert client.fills.await_args.args[0] == "BTCUSDT"
    # v2: one flat-to-flat episode, attributed through the journal / ledger entry order
    assert report["strategies"]["iceberg_detector"]["wins"] == 1
    (episode,) = report["episodes"]
    assert episode["status"] == "attributed"
    assert episode["net_pnl"] == pytest.approx(2.0)
    assert episode["ledger_rows"][0]["strategy_position_id"] == "sp1"
    assert report["metadata"]["symbols"] == ["BTCUSDT"]
    assert report["metadata"]["method"] == "exchange flat-to-flat episodes (v2)"


async def test_compute_needs_both_stores() -> None:
    db = type("Db", (), {"mongodb_adapter": None, "mysql_adapter": None})()
    with pytest.raises(GroundTruthInputError):
        await compute_ground_truth(db, window_days=7, client=AsyncMock(), now=NOW)


async def test_ledger_entry_order_maps_a_strategy_the_journal_lacks() -> None:
    entry_ms = int((NOW - timedelta(days=1)).timestamp() * 1000)
    rows = [
        {
            "strategy_position_id": "sp9",
            "strategy_id": "inside_bar_breakout",
            "symbol": "ETHUSDT",
            "side": "LONG",
            "status": "closed_externally",
            "entry_order_id": "777",
            "entry_time": NOW - timedelta(days=1),
        }
    ]
    client = AsyncMock()
    client.fills.return_value = [
        {
            "id": 1,
            "orderId": 777,
            "symbol": "ETHUSDT",
            "side": "BUY",
            "price": "2000",
            "qty": "0.01",
            "realizedPnl": "0",
            "commission": "0",
            "time": entry_ms,
        },
        {
            "id": 2,
            "orderId": 888,
            "symbol": "ETHUSDT",
            "side": "SELL",
            "price": "2010",
            "qty": "0.01",
            "realizedPnl": "0.1",
            "commission": "0",
            "time": entry_ms + 60_000,
        },
    ]
    client.orders.return_value = []
    db = type(
        "Db", (), {"mongodb_adapter": _Mongo(rows), "mysql_adapter": _MySQL([])}
    )()

    report = await compute_ground_truth(db, window_days=7, client=client, now=NOW)

    assert report["episodes"][0]["strategy_id"] == "inside_bar_breakout"
    assert report["external_closes"] == {"inside_bar_breakout": {"unknown": 1}}
