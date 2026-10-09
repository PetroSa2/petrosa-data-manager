import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_manager.api.routes.analysis import (
    REGIME_DOC_PROJECTION,
    SLIPPAGE_FILL_PROJECTION,
    compute_closed_rounds,
    compute_slippage_by_regime,
)
from data_manager.consumer.execution_events_consumer import ExecutionEventsConsumer
from data_manager.models.execution_event import ExecutionEvent
from data_manager.services.report_precompute import ReportPrecomputer
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

    assert projections[0] == SLIPPAGE_FILL_PROJECTION
    assert "payload.slippage_bp" in projections[0]
    assert "payload.intended_price" in projections[0]
    # slippage_report falls back to a top-level regime_at_fill, so it must be fetched too
    assert "regime_at_fill" in projections[0]
    assert "payload.regime_at_fill" in projections[0]


@pytest.mark.asyncio
async def test_regime_docs_use_the_shared_projection_and_rounds_stay_unprojected():
    seen: dict[str, object] = {}

    class Cursor:
        def sort(self, *args):
            return self

        async def to_list(self, length=None):
            return [
                {
                    "event_type": "filled",
                    "symbol": "BTCUSDT",
                    "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
                    "payload": {"slippage_bp": 1.0},
                }
            ]

    class Collection:
        def __init__(self, name):
            self.name = name

        def find(self, *args):
            seen[self.name] = args
            return Cursor()

    class Database(dict):
        def __getitem__(self, name):
            return Collection(name)

    manager = SimpleNamespace(mongodb_adapter=SimpleNamespace(db=Database()))
    await compute_slippage_by_regime(manager)
    await compute_closed_rounds(manager)

    assert seen["analytics_BTCUSDT_regime"] == ({}, REGIME_DOC_PROJECTION)
    # rounds reads whole events (see the note above SLIPPAGE_FILL_PROJECTION): no projection argument
    assert len(seen["execution_events"]) == 1


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


def _seed_candles(monkeypatch, symbol: str = "BTCUSDT") -> None:
    """Seed one pair with daily and hourly candles so the risk inputs are non-empty."""
    import math
    from datetime import timedelta

    from data_manager.db.repositories import CandleRepository

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    midnight = now.replace(hour=0)
    candles = {
        "1d": [
            {
                "timestamp": midnight - timedelta(days=day),
                "close": 100 * (1 + 0.02 * math.sin(day)),
            }
            for day in range(40)
        ],
        "1h": [
            {
                "timestamp": now - timedelta(hours=hour),
                "close": 100 * (1 + 0.01 * math.sin(hour / 3)),
            }
            for hour in range(24 * 20)
        ],
    }

    async def get_range(self, pair, timeframe, start, end, **kwargs):
        assert pair == symbol
        return [c for c in candles[timeframe] if start <= c["timestamp"] <= end]

    monkeypatch.setattr(CandleRepository, "get_range", get_range)


@pytest.mark.asyncio
async def test_leader_run_refreshes_every_report_without_mocked_compute(monkeypatch):
    """One non-mocked leader iteration: the real compute functions fill the cache."""
    import data_manager.api.app as api_module
    from data_manager.api.routes import risk as risk_route
    from data_manager.services import report_precompute

    now = datetime.now(UTC)
    fills = [
        {
            "event_type": "filled",
            "symbol": "BTCUSDT",
            "strategy_id": "strategy",
            "timestamp": now,
            "fill_time": now,
            "side": side,
            "position_side": "LONG",
            "fill_qty": 1,
            "fill_price": price,
            "payload": {"slippage_bp": bp},
        }
        for side, price, bp in (("BUY", 100, 2.0), ("SELL", 101, 4.0))
    ]

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def sort(self, *args):
            return self

        async def to_list(self, length=None):
            return self.rows

    class Collection:
        def __init__(self, rows=None):
            self.rows = rows or []

        def find(self, query=None, projection=None):
            return Cursor(self.rows)

        async def find_one(self, query, sort=None):
            return None

    class Database(dict):
        def __getitem__(self, name):
            if name == "execution_events":
                return Collection(fills)
            if name.startswith("analytics_"):
                return Collection([{"regime": "balanced_market", "computed_at": now}])
            return super().__getitem__(name)

    cache = FakeCollection()
    manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db=Database(report_cache=cache)),
        mysql_adapter=None,
    )
    monkeypatch.setattr(api_module, "db_manager", manager)
    monkeypatch.setattr(risk_route, "_load_wallet_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("constants.SUPPORTED_PAIRS", ("BTCUSDT",))
    _seed_candles(monkeypatch)
    precomputer = ReportPrecomputer(manager, SimpleNamespace(is_leader=True))
    precomputer.running = True

    async def stop_after_the_first_iteration(_seconds):
        precomputer.running = False

    monkeypatch.setattr(
        report_precompute.asyncio, "sleep", stop_after_the_first_iteration
    )
    await precomputer._run()

    assert set(cache.rows) == {
        "slippage_by_regime:window_days=30",
        "rounds:window_days=30",
        "risk_inputs:window_days=30",
    }
    bodies = {key: row["body"] for key, row in cache.rows.items()}
    assert bodies["slippage_by_regime:window_days=30"]["overall"]["count"] == 2
    assert (
        bodies["rounds:window_days=30"]["strategies"]["strategy"]["closed_rounds"] == 1
    )
    risk = bodies["risk_inputs:window_days=30"]
    assert risk["symbols"]["BTCUSDT"]["sufficient"] is True
    assert risk["symbols_unavailable"] == []


@pytest.mark.asyncio
async def test_non_leader_does_not_refresh(monkeypatch):
    from data_manager.services import report_precompute

    cache = FakeCollection()
    manager = SimpleNamespace(mongodb_adapter=FakeMongo(cache))
    precomputer = ReportPrecomputer(manager, SimpleNamespace(is_leader=False))
    precomputer.running = True

    async def stop(_seconds):
        precomputer.running = False

    monkeypatch.setattr(report_precompute.asyncio, "sleep", stop)
    await precomputer._run()

    assert cache.rows == {}


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
                return Collection([{"regime": "balanced_market", "computed_at": now}])
            return super().__getitem__(name)

    cache = FakeCollection()
    database = Database(report_cache=cache)
    manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db=database), mysql_adapter=None
    )
    precomputer = ReportPrecomputer(manager)
    monkeypatch.setattr(api_module, "db_manager", manager)
    monkeypatch.setattr(api_module, "report_precomputer", precomputer)
    monkeypatch.setattr(risk_route, "_load_wallet_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("constants.SUPPORTED_PAIRS", ("BTCUSDT",))
    _seed_candles(monkeypatch)

    assert (await get_slippage_by_regime(30.0, None, None))["overall"]["count"] == 2
    rounds = await get_closed_rounds(None, 30.0)
    assert rounds["strategies"]["strategy"]["closed_rounds"] == 1
    risk = await get_risk_inputs(30, 14, 60, 4.0, None, 30.0)
    assert risk["symbols_unavailable"] == []
    assert risk["symbols"]["BTCUSDT"]["daily"]["n_returns"] >= 20
    assert risk["symbols"]["BTCUSDT"]["sufficient"] is True
    assert set(cache.rows) == {
        "slippage_by_regime:window_days=30",
        "rounds:window_days=30",
        "risk_inputs:window_days=30",
    }


def apply_projection(document: dict, projection: dict) -> dict:
    """What MongoDB returns for an inclusion projection (dotted paths, no ``_id``)."""
    out: dict = {}
    for path in projection:
        parts = path.split(".")
        source, target = document, out
        for part in parts[:-1]:
            if not isinstance(source, dict) or part not in source:
                break
            source = source[part]
            target = target.setdefault(part, {})
        else:
            if isinstance(source, dict) and parts[-1] in source:
                target[parts[-1]] = deepcopy(source[parts[-1]])
    return out


def _production_fills() -> list[dict]:
    day = datetime(2026, 10, 6, tzinfo=UTC)

    def fill(hour, side, price, **extra):
        row = {
            "_id": f"id-{hour}-{side}",
            "event_type": "filled",
            "symbol": "BTCUSDT",
            "strategy_id": "strategy",
            "decision_id": "decision",
            "order_id": f"order-{hour}",
            "timestamp": day.replace(hour=hour),
            "fill_time": day.replace(hour=hour),
            "side": side,
            "position_side": "LONG",
            "fill_qty": 1,
            "fill_price": price,
            "fee": 0.1,
            "fee_asset": "USDT",
            "payload": {
                "client_order_id": f"c-{hour}",
                "venue_trade_id": 12345 + hour,
                "raw": {"a": 1, "nested": {"b": [1, 2, 3]}},
            },
        }
        payload_extra = extra.pop("payload", {})
        row.update(extra)
        row["payload"].update(payload_extra)
        return row

    return [
        # telemetry in the payload, regime from the timeline
        fill(0, "BUY", 100, payload={"slippage_bp": 2.0, "role": "entry"}),
        # telemetry on the event itself (legacy rows), role from reduce_only
        fill(1, "SELL", 101, slippage_bp=4.0, reduce_only=True),
        # a stamped regime in the payload and one on the event
        fill(2, "BUY", 100, payload={"slippage_bp": 1.0, "regime_at_fill": "stamped"}),
        fill(3, "SELL", 99, slippage_bp=6.0, regime_at_fill="top_level_stamp"),
        # emitted null for want of an intended price, and no telemetry at all
        fill(4, "BUY", 100, payload={"slippage_bp": None, "intended_price": None}),
        fill(5, "SELL", 100),
        # a partial fill and an other-event row that must be ignored
        fill(6, "BUY", 100, event_type="partial_fill", payload={"slippage_bp": 3.0}),
        fill(7, "BUY", 100, event_type="placed", payload={"slippage_bp": 9.0}),
    ]


def _production_regimes() -> dict[str, list[dict]]:
    day = datetime(2026, 10, 6, tzinfo=UTC)
    return {
        "BTCUSDT": [
            {
                "_id": "r1",
                "regime": "balanced_market",
                "metadata": {"computed_at": day, "extra": {"x": 1}},
                "confidence": 0.9,
                "features": {"adx": 20.0},
            },
            {
                "_id": "r2",
                "regime": "turbulent_illiquidity",
                "computed_at": day.replace(hour=3),
                "metadata": {"source": "x"},
            },
            {"_id": "r3", "regime": "trend", "timestamp": day.replace(hour=5)},
        ]
    }


def test_slippage_report_is_identical_for_the_real_projections():
    fills, regimes = _production_fills(), _production_regimes()
    projected_fills = [apply_projection(row, SLIPPAGE_FILL_PROJECTION) for row in fills]
    projected_regimes = {
        symbol: [apply_projection(doc, REGIME_DOC_PROJECTION) for doc in docs]
        for symbol, docs in regimes.items()
    }

    full = build_slippage_report(fills, regimes)
    projected = build_slippage_report(projected_fills, projected_regimes)

    assert projected == full
    # the fixture exercises every path the projection has to keep
    assert {"balanced_market", "stamped", "top_level_stamp"} <= set(full["by_regime"])
    assert full["fills_without_cost_telemetry"] == 1
    assert full["fills_without_intended_price"] == 1
    assert full["fills_with_slippage"] == 5
    for role in ("entry", "exit"):
        assert build_slippage_report(
            projected_fills, projected_regimes, role=role
        ) == build_slippage_report(fills, regimes, role=role)


def test_a_projection_without_the_top_level_regime_stamp_would_change_the_report():
    """The golden test is sensitive: dropping ``regime_at_fill`` from the projection is caught."""
    fills, regimes = _production_fills(), _production_regimes()
    narrower = {
        k: v for k, v in SLIPPAGE_FILL_PROJECTION.items() if k != "regime_at_fill"
    }
    projected = [apply_projection(row, narrower) for row in fills]

    assert build_slippage_report(projected, regimes) != build_slippage_report(
        fills, regimes
    )
