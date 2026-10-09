import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_manager.api.routes.analysis import compute_slippage_by_regime
from data_manager.consumer.execution_events_consumer import ExecutionEventsConsumer
from data_manager.models.execution_event import ExecutionEvent
from data_manager.services.report_precompute import ReportPrecomputer
from data_manager.services.round_book import build_report as build_round_report
from data_manager.services.slippage_report import build_report as build_slippage_report


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


@pytest.mark.asyncio
async def test_cache_hit_keeps_timezone_aware_mongodb_datetime():
    collection = FakeCollection()
    computed_at = datetime.now(UTC)
    collection.rows["risk_inputs:window_days=30"] = {
        "body": {"metadata": {}},
        "computed_at": computed_at,
    }

    served = await ReportPrecomputer(
        SimpleNamespace(mongodb_adapter=FakeMongo(collection))
    ).get("risk_inputs", window_days=30)

    assert served["metadata"]["computed_at"] == computed_at.isoformat()


@pytest.mark.asyncio
async def test_precomputer_start_and_stop_manage_the_refresh_task(monkeypatch):
    collection = FakeCollection()
    manager = SimpleNamespace(mongodb_adapter=FakeMongo(collection))
    precomputer = ReportPrecomputer(manager)
    monkeypatch.setattr("constants.ENABLE_REPORT_PRECOMPUTE", True)
    precomputer._run = AsyncMock()

    await precomputer.start()
    await asyncio.sleep(0)
    await precomputer.stop()

    assert precomputer.task is None
    precomputer._run.assert_awaited_once()


@pytest.mark.asyncio
async def test_precomputer_refreshes_each_report_with_explicit_defaults(monkeypatch):
    collection = FakeCollection()
    manager = SimpleNamespace(mongodb_adapter=FakeMongo(collection))
    precomputer = ReportPrecomputer(manager)
    precomputer.running = True
    monkeypatch.setattr("constants.REPORT_SLIPPAGE_INTERVAL_SECONDS", 900)
    monkeypatch.setattr("constants.REPORT_RISK_INTERVAL_SECONDS", 300)

    import data_manager.api.routes.analysis as analysis
    import data_manager.api.routes.risk as risk

    monkeypatch.setattr(
        analysis, "compute_slippage_by_regime", AsyncMock(return_value={})
    )
    monkeypatch.setattr(analysis, "compute_closed_rounds", AsyncMock(return_value={}))
    monkeypatch.setattr(risk, "compute_risk_inputs", AsyncMock(return_value={}))

    task = asyncio.create_task(precomputer._run())
    await asyncio.sleep(0)
    precomputer.running = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert set(collection.rows) == {
        "slippage_by_regime:window_days=30",
        "rounds:window_days=30",
        "risk_inputs:window_days=30",
    }


@pytest.mark.asyncio
async def test_regime_refresh_populates_cache_off_the_fill_path(monkeypatch):
    class Cursor:
        async def to_list(self, length=None):
            return [{"regime": "balanced_market"}]

    class Collection:
        def find(self, query):
            return Cursor()

    class Database(dict):
        def __getitem__(self, name):
            return Collection()

    manager = SimpleNamespace(mongodb_adapter=SimpleNamespace(db=Database()))
    consumer = ExecutionEventsConsumer(db_manager=manager)
    consumer.running = True

    async def stop_after_first_sleep(_seconds):
        consumer.running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_sleep)
    await consumer._refresh_regimes()

    assert "BTCUSDT" in consumer._regime_cache


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
        [
            {
                "regime": "balanced_market",
                "computed_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ],
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


@pytest.mark.asyncio
async def test_default_routes_serve_cached_reports_without_computing(monkeypatch):
    import data_manager.api.app as api_module
    from data_manager.api.routes.analysis import (
        get_closed_rounds,
        get_slippage_by_regime,
    )
    from data_manager.api.routes.risk import get_risk_inputs

    precomputer = SimpleNamespace(
        get_or_compute=AsyncMock(return_value={"cached": True})
    )
    monkeypatch.setattr(api_module, "report_precomputer", precomputer)

    assert (await get_slippage_by_regime(30.0, None, None)) == {"cached": True}
    assert (await get_closed_rounds(None, 30.0)) == {"cached": True}
    assert (await get_risk_inputs(30, 14, 60, 4.0, None, 30.0)) == {"cached": True}
    assert precomputer.get_or_compute.await_count == 3


@pytest.mark.asyncio
async def test_cold_miss_single_flight_computes_once():
    collection = FakeCollection()
    precomputer = ReportPrecomputer(
        SimpleNamespace(mongodb_adapter=FakeMongo(collection))
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _report_body()

    first = asyncio.create_task(
        precomputer.get_or_compute("slippage_by_regime", compute, window_days=30)
    )
    await started.wait()
    assert (
        await precomputer.get_or_compute("slippage_by_regime", compute, window_days=30)
    ) is None
    release.set()
    assert (await first)["overall"]["count"] == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_real_refresh_is_served_by_each_default_route(monkeypatch):
    import data_manager.api.app as api_module
    from data_manager.api.routes import risk as risk_route
    from data_manager.api.routes.analysis import (
        get_closed_rounds,
        get_slippage_by_regime,
    )
    from data_manager.api.routes.risk import get_risk_inputs

    now = datetime.now(UTC)
    fills = [
        {
            "event_type": "filled",
            "symbol": "BTCUSDT",
            "strategy_id": "strategy",
            "timestamp": now,
            "fill_time": now,
            "side": "BUY",
            "position_side": "LONG",
            "fill_qty": 1,
            "fill_price": 100,
            "payload": {"slippage_bp": 2.0},
        },
        {
            "event_type": "filled",
            "symbol": "BTCUSDT",
            "strategy_id": "strategy",
            "timestamp": now,
            "fill_time": now,
            "side": "SELL",
            "position_side": "LONG",
            "fill_qty": 1,
            "fill_price": 101,
            "payload": {"slippage_bp": 4.0},
        },
    ]

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def sort(self, *args):
            return self

        async def to_list(self, length=None):
            return self.rows[:length] if length else self.rows

    class Collection:
        def __init__(self, rows=None):
            self.rows = rows or []

        def find(self, query=None, projection=None):
            return Cursor(self.rows)

        async def find_one(self, query, sort=None):
            return None

        async def create_index(self, *args, **kwargs):
            return "computed_at_ttl"

        async def replace_one(self, query, document, upsert=False):
            self.rows = [document]

    class Database(dict):
        def __getitem__(self, name):
            if name == "execution_events":
                return Collection(fills)
            if name.startswith("analytics_"):
                return Collection(
                    [{"regime": "balanced_market", "computed_at": now}]
                )
            return super().__getitem__(name)

    cache = FakeCollection()
    database = Database(report_cache=cache)
    manager = SimpleNamespace(mongodb_adapter=SimpleNamespace(db=database))
    precomputer = ReportPrecomputer(manager)
    monkeypatch.setattr(api_module, "db_manager", manager)
    monkeypatch.setattr(api_module, "report_precomputer", precomputer)
    monkeypatch.setattr(risk_route, "_load_wallet_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("constants.SUPPORTED_PAIRS", ())

    assert (await get_slippage_by_regime(30.0, None, None))["overall"]["count"] == 2
    rounds = await get_closed_rounds(None, 30.0)
    assert rounds["strategies"]["strategy"]["closed_rounds"] == 1
    risk = await get_risk_inputs(30, 14, 60, 4.0, None, 30.0)
    assert risk["symbols"] == {}
    assert set(cache.rows) == {
        "slippage_by_regime:window_days=30",
        "rounds:window_days=30",
        "risk_inputs:window_days=30",
    }


def test_slippage_and_round_reports_match_for_full_and_projected_rows():
    regime_time = datetime(2026, 10, 6, tzinfo=UTC)
    fills = [
        {
            "event_type": "filled",
            "symbol": "BTCUSDT",
            "strategy_id": "strategy",
            "timestamp": regime_time,
            "fill_time": regime_time,
            "side": "BUY",
            "position_side": "LONG",
            "fill_qty": 1,
            "fill_price": 100,
            "fee": 0.1,
            "fees": [{"amount": 0.1, "asset": "USDT"}],
            "fee_asset": "USDT",
            "fee_status": "confirmed",
            "position_id": "position",
            "decision_id": "decision",
            "reason": "entry",
            "payload": {"slippage_bp": 2.0, "role": "entry"},
        },
        {
            "event_type": "filled",
            "symbol": "BTCUSDT",
            "strategy_id": "strategy",
            "timestamp": regime_time,
            "fill_time": regime_time.replace(hour=1),
            "side": "SELL",
            "position_side": "LONG",
            "fill_qty": 1,
            "fill_price": 101,
            "fee": 0.1,
            "fees": [{"amount": 0.1, "asset": "USDT"}],
            "fee_asset": "USDT",
            "fee_status": "confirmed",
            "position_id": "position",
            "decision_id": "decision",
            "reason": "oco_exit_1",
            "payload": {"slippage_bp": 4.0, "role": "exit"},
        },
    ]
    regimes = {
        "BTCUSDT": [
            {"regime": "balanced_market", "metadata": {"computed_at": regime_time}}
        ]
    }
    slippage_projected = [
        {
            key: deepcopy(row[key])
            for key in ("event_type", "symbol", "timestamp", "fill_time", "payload")
        }
        for row in fills
    ]
    rounds_projected = [
        {
            key: deepcopy(row[key])
            for key in (
                "event_type",
                "symbol",
                "strategy_id",
                "timestamp",
                "fill_time",
                "side",
                "position_side",
                "fill_qty",
                "fill_price",
                "fee",
                "fees",
                "fee_asset",
                "fee_status",
                "position_id",
                "decision_id",
                "reason",
                "payload",
            )
        }
        for row in fills
    ]

    assert build_slippage_report(fills, regimes) == build_slippage_report(
        slippage_projected, regimes
    )
    assert build_round_report(fills, window_days=30) == build_round_report(
        rounds_projected, window_days=30
    )
