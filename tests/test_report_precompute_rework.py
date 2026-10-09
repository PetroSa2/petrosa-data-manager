from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from data_manager.api.routes.analysis import compute_slippage_by_regime
from data_manager.consumer.execution_events_consumer import ExecutionEventsConsumer
from data_manager.models.execution_event import ExecutionEvent
from data_manager.services.report_precompute import ReportPrecomputer


class FakeCollection:
    def __init__(self):
        self.rows = {}

    async def create_index(self, *args, **kwargs):
        return "computed_at_ttl"

    async def find_one(self, query):
        return self.rows.get(query["_id"])

    async def replace_one(self, query, document, upsert=False):
        self.rows[query["_id"]] = document


class FakeMongo:
    def __init__(self, collection):
        self.db = {"report_cache": collection}


@pytest.mark.asyncio
async def test_refresh_stores_a_default_report_that_is_served():
    collection = FakeCollection()
    manager = SimpleNamespace(mongodb_adapter=FakeMongo(collection))
    precomputer = ReportPrecomputer(manager)

    async def refresh():
        return _report_body()

    await precomputer._refresh("slippage_by_regime", refresh)

    served = await precomputer.get("slippage_by_regime", window_days=30)

    assert served["overall"]["count"] == 1
    assert served["metadata"]["stale"] is False
    assert served["metadata"]["computed_at"] == served["metadata"]["calculated_at"]


@pytest.mark.asyncio
async def test_cache_hit_normalizes_naive_mongodb_datetime():
    collection = FakeCollection()
    collection.rows["risk_inputs:window_days=30"] = {
        "body": {"metadata": {}},
        "computed_at": datetime.now(UTC).replace(tzinfo=None),
    }
    manager = SimpleNamespace(mongodb_adapter=FakeMongo(collection))

    served = await ReportPrecomputer(manager).get("risk_inputs", window_days=30)

    assert served["metadata"]["age_seconds"] >= 0
    assert served["metadata"]["computed_at"].endswith("+00:00")


def _report_body():
    return {"overall": {"count": 1}}


@pytest.mark.asyncio
async def test_fill_regime_stamp_is_present_on_the_event_sent_to_mysql(monkeypatch):
    inserted = []
    mysql_events = []

    class Collection:
        async def insert_one(self, document):
            inserted.append(document)

        async def update_one(self, *args, **kwargs):
            return None

    class Database(dict):
        def __getitem__(self, name):
            return Collection()

    class MongoAdapter:
        db = Database()

        @staticmethod
        def _prepare_for_bson(document):
            return document

    class MysqlAdapter:
        def write(self, events, collection):
            mysql_events.extend(events)

    manager = SimpleNamespace(
        mongodb_adapter=MongoAdapter(), mysql_adapter=MysqlAdapter()
    )
    consumer = ExecutionEventsConsumer(db_manager=manager)
    consumer._regime_cache["BTCUSDT"] = (
        datetime.now(UTC),
        [{"regime": "balanced_market", "computed_at": datetime(2026, 1, 1, tzinfo=UTC)}],
    )
    event = ExecutionEvent(
        decision_id="decision",
        strategy_id="strategy",
        order_id="order",
        event_type="filled",
        timestamp=datetime(2026, 1, 2, tzinfo=UTC),
        fill_time=datetime(2026, 1, 2, tzinfo=UTC),
        symbol="BTCUSDT",
    )
    monkeypatch.setenv("PETROSA_EXECUTION_EVENTS_MYSQL_PERSIST_ENABLED", "false")

    assert await consumer._persist(event) is True

    assert inserted[0]["payload"]["regime_at_fill"] == "balanced_market"
    await consumer._dual_write_mysql(event)
    assert mysql_events[0].payload["regime_at_fill"] == "balanced_market"


@pytest.mark.asyncio
async def test_slippage_projection_preserves_telemetry_and_regime_time_fields():
    projections = []

    class Cursor:
        def sort(self, *args):
            return self

        async def to_list(self, length=None):
            return []

    class Collection:
        def find(self, query, projection=None):
            projections.append(projection)
            return Cursor()

    class Database(dict):
        def __getitem__(self, name):
            return Collection()

    manager = SimpleNamespace(mongodb_adapter=SimpleNamespace(db=Database()))

    await compute_slippage_by_regime(manager)

    assert "payload.slippage_bp" in projections[0]
    assert "payload.intended_price" in projections[0]
