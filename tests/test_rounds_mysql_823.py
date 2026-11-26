"""petrosa-data-manager#823: round fills come from the MySQL historic copy plus uncopied Mongo docs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pymongo.errors import NetworkTimeout

from data_manager.api.routes import analysis

NOW = datetime.now(UTC)


def _mysql_row(
    order_id, side, qty, price, minutes_ago, strategy="order_flow_imbalance"
):
    return {
        "event_key": f"{order_id}:filled",
        "decision_id": f"d-{order_id}",
        "strategy_id": strategy,
        "order_id": order_id,
        "event_type": "filled",
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
        "reason": None,
        "symbol": "BTCUSDT",
        "side": side,
        "qty": Decimal(str(qty)),
        "fill_qty": Decimal(str(qty)),
        "fill_quantity": None,
        "price": Decimal(str(price)),
        "fill_price": Decimal(str(price)),
        "fill_time": None,
        "fee": Decimal("0.01"),
        "fee_asset": "USDT",
        "pnl": None,
        "payload": json.dumps({"position_side": "LONG"}),
    }


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    def __init__(self, mysql):
        self.mysql = mysql

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params):
        self.mysql.calls.append((str(statement), params))
        if self.mysql.error:
            raise self.mysql.error
        return _Result(self.mysql.rows)


class FakeMySQL:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls: list = []

    def _ensure_connected(self):
        return SimpleNamespace(begin=lambda: _Connection(self))


class _Cursor:
    def __init__(self, collection, query):
        self.collection, self.query = collection, query

    def sort(self, *_args):
        return self

    async def to_list(self, length):
        self.collection.queries.append(self.query)
        if self.collection.error and "mysql_copied_at" in self.query:
            raise self.collection.error
        return list(self.collection.docs)


class FakeCollection:
    def __init__(self, docs=None, error=None):
        self.docs = docs or []
        self.error = error
        self.queries: list = []

    def find(self, query, projection=None):
        return _Cursor(self, query)


def _manager(collection, mysql):
    mongodb = SimpleNamespace(
        analytics_db={"execution_events": collection}, db=object()
    )
    return SimpleNamespace(mongodb_adapter=mongodb, mysql_adapter=mysql)


@pytest.fixture(autouse=True)
def _overlay_off(monkeypatch):
    monkeypatch.setenv("ROUND_ORPHAN_MARKING", "off")


def test_mysql_row_takes_the_mongo_document_shape():
    doc = analysis._mysql_round_row(_mysql_row("o1", "BUY", 0.01, 50000, 5))
    assert doc["fill_qty"] == pytest.approx(0.01) and isinstance(doc["fill_qty"], float)
    assert doc["timestamp"].tzinfo is not None
    assert doc["payload"] == {"position_side": "LONG"}
    bad = analysis._mysql_round_row(
        {**_mysql_row("o2", "BUY", 1, 1, 1), "payload": "{"}
    )
    assert bad["payload"] == {}


def test_merge_keeps_each_event_once_in_time_order():
    later = {"order_id": "a", "event_type": "filled", "timestamp": NOW, "src": "mysql"}
    earlier = {"order_id": "b", "event_type": "filled", "timestamp": NOW - timedelta(1)}
    dup = {"order_id": "a", "event_type": "filled", "timestamp": NOW, "src": "mongo"}
    merged = analysis._merge_round_fills([later], [dup, earlier], cap=10)
    assert [r["order_id"] for r in merged] == ["b", "a"]
    assert merged[1]["src"] == "mysql"
    assert len(analysis._merge_round_fills([later], [earlier], cap=1)) == 1


@pytest.mark.asyncio
async def test_rounds_read_mysql_and_only_uncopied_mongo_docs():
    mysql = FakeMySQL(
        [
            _mysql_row("o1", "BUY", 0.01, 50000, 120),
            _mysql_row("o2", "SELL", 0.01, 50100, 60),
        ]
    )
    collection = FakeCollection()
    report = await analysis.compute_closed_rounds(
        _manager(collection, mysql), "order_flow_imbalance", 90
    )

    sql, params = mysql.calls[0]
    assert "FROM execution_events" in sql and "strategy_id = :strategy_id" in sql
    assert params["strategy_id"] == "order_flow_imbalance"
    assert params["events"] == ["filled", "partial_fill"]
    assert params["start"].tzinfo is None  # MySQL stores naive UTC
    [mongo_query] = collection.queries
    assert mongo_query["mysql_copied_at"] == {"$exists": False}
    assert report["metadata"]["fills_read"] == 2
    assert report["metadata"]["fills_source"] == "mysql"
    assert report["metadata"]["uncopied_read"] == "ok"
    assert report["strategies"]["order_flow_imbalance"]["fills"] == 2
    assert report.get("status") != "partial"


@pytest.mark.asyncio
async def test_mysql_failure_falls_back_to_the_mongo_read():
    mongo_fill = analysis._mysql_round_row(_mysql_row("m1", "BUY", 0.01, 50000, 30))
    collection = FakeCollection(docs=[mongo_fill])
    report = await analysis.compute_closed_rounds(
        _manager(collection, FakeMySQL(error=RuntimeError("mysql down"))),
        "order_flow_imbalance",
        90,
    )
    [mongo_query] = collection.queries
    assert "mysql_copied_at" not in mongo_query
    assert report["metadata"]["fills_source"] == "mongo"
    assert report["metadata"]["fills_read"] == 1


@pytest.mark.asyncio
async def test_uncopied_timeout_keeps_mysql_rows_and_reports_partial():
    mysql = FakeMySQL([_mysql_row("o1", "BUY", 0.01, 50000, 120)])
    collection = FakeCollection(error=NetworkTimeout("slow atlas"))
    report = await analysis.compute_closed_rounds(
        _manager(collection, mysql), "order_flow_imbalance", 90
    )
    assert report["metadata"]["fills_read"] == 1
    assert report["metadata"]["uncopied_read"] == "timed_out"
    assert report["status"] == "partial"
