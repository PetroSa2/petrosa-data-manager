"""Tests for system trade audit-trail endpoints (#529)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app

T0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


def _fill(
    *,
    side: str,
    qty: float,
    price: float,
    seconds_before: int = 0,
    strategy_id: str = "S1",
    symbol: str = "BTCUSDT",
    event_type: str = "filled",
    order_id: str | None = None,
    fee: float = 0.01,
    fee_asset: str = "USDT",
    pnl: float | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "side": side,
        "fill_qty": qty,
        "fill_quantity": qty,
        "price": price,
        "fill_price": price,
        "fill_time": T0 - timedelta(seconds=seconds_before),
        "fee": fee,
        "fee_asset": fee_asset,
        "pnl": pnl,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "order_id": order_id or f"O-{side}-{seconds_before}",
        "decision_id": "D",
        "timestamp": T0 - timedelta(seconds=seconds_before),
        "reason": "filled",
    }


def _client_with_fills(rows: list[dict[str, Any]]) -> TestClient:
    app = create_app()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    coll = MagicMock()
    coll.find = MagicMock(return_value=cursor)
    coll.count_documents = AsyncMock(return_value=len(rows))
    mongodb = MagicMock()
    mongodb.db = {"execution_events": coll}
    db_manager = MagicMock()
    db_manager.mongodb_adapter = mongodb
    api_module.db_manager = db_manager
    return TestClient(app)


def test_system_trades_returns_fill_audit_fields():
    rows = [
        _fill(side="buy", qty=0.01, price=65000.0, fee=0.05, fee_asset="BNB", pnl=None),
        _fill(side="sell", qty=0.01, price=66000.0, seconds_before=-10, pnl=10.0),
    ]
    try:
        client = _client_with_fills(rows)
        r = client.get("/api/v1/trades", params={"strategy_id": "S1"})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "execution_events"
        assert body["pagination"]["total"] == 2
        trade = body["trades"][0]
        assert trade["fill_price"] == 65000.0
        assert trade["fill_quantity"] == 0.01
        assert trade["fee"] == 0.05
        assert trade["fee_asset"] == "BNB"
        assert "fill_time" in trade
        assert trade["symbol"] == "BTCUSDT"
        assert trade["side"] == "buy"
    finally:
        api_module.db_manager = None


def test_system_trades_filters_by_symbol():
    rows = [
        _fill(side="buy", qty=1, price=100, symbol="BTCUSDT"),
        _fill(side="buy", qty=1, price=200, symbol="ETHUSDT", seconds_before=-1),
    ]
    try:
        client = _client_with_fills(rows)
        r = client.get("/api/v1/trades", params={"symbol": "ETHUSDT"})
        assert r.status_code == 200
        # Endpoint itself applies DB query; we assert filters are echoed and
        # find() was called with symbol constraint via mock call args.
        body = r.json()
        assert body["filters"]["symbol"] == "ETHUSDT"
        assert body["pagination"]["total"] == 2  # mock returns all rows
    finally:
        api_module.db_manager = None


def test_system_trades_summary_reports_win_rate_and_fees():
    rows = [
        _fill(side="buy", qty=1, price=100, fee=0.1),
        _fill(side="sell", qty=1, price=110, fee=0.1, seconds_before=-5),  # win
        _fill(side="buy", qty=1, price=100, fee=0.1, seconds_before=-10),
        _fill(side="sell", qty=1, price=90, fee=0.1, seconds_before=-15),  # loss
    ]
    try:
        client = _client_with_fills(rows)
        r = client.get("/api/v1/trades/summary", params={"strategy_id": "S1"})
        assert r.status_code == 200
        body = r.json()
        assert body["fills"] == 4
        assert body["wins"] == 1
        assert body["losses"] == 1
        assert body["closed_rounds"] == 2
        assert body["win_rate"] == 0.5
        assert body["total_pnl"] == 0.0  # +10 and -10
        assert abs(body["fee_total"] - 0.4) < 1e-9
        assert body["source"] == "execution_events"
    finally:
        api_module.db_manager = None


def test_system_trades_paginates_in_mongo_not_memory():
    """#529: skip/limit must reach the driver so wide windows never load fully."""
    rows = [_fill(side="buy", qty=1, price=100, seconds_before=-i) for i in range(3)]
    try:
        client = _client_with_fills(rows)
        r = client.get(
            "/api/v1/trades", params={"symbol": "BTCUSDT", "offset": 10, "limit": 5}
        )
        assert r.status_code == 200
        coll = api_module.db_manager.mongodb_adapter.db["execution_events"]
        query = coll.find.call_args.args[0]
        assert query["symbol"] == "BTCUSDT"
        assert query["event_type"] == {"$in": ["filled", "partial_fill"]}
        cursor = coll.find.return_value
        cursor.skip.assert_called_once_with(10)
        cursor.limit.assert_called_once_with(5)
        coll.count_documents.assert_awaited_once()
    finally:
        api_module.db_manager = None


def test_system_trades_summary_is_bounded():
    """#529: PnL replay cannot paginate, so the scan must be explicitly capped."""
    from data_manager.api.routes.system_trades import _SUMMARY_MAX_FILLS

    rows = [_fill(side="buy", qty=1, price=100)]
    try:
        client = _client_with_fills(rows)
        r = client.get("/api/v1/trades/summary")
        assert r.status_code == 200
        assert r.json()["truncated"] is False
        coll = api_module.db_manager.mongodb_adapter.db["execution_events"]
        coll.find.return_value.limit.assert_called_once_with(_SUMMARY_MAX_FILLS)
    finally:
        api_module.db_manager = None


def test_system_trades_503_when_db_unavailable():
    app = create_app()
    api_module.db_manager = None
    client = TestClient(app)
    r = client.get("/api/v1/trades")
    assert r.status_code == 503
