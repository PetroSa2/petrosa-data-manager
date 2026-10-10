"""Calibration with the MySQL decision history (petrosa-data-manager#585).

Mongo ``cio_decisions`` keeps one day; the permanent copy is in MySQL. The engine below is SQLite with
``check_same_thread=False`` because the lookup really runs in a worker thread.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from bson import BSON
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient
from sqlalchemy import event

import data_manager.api.app as api_module
import data_manager.services.calibration_service as calibration
from data_manager.api.routes.analysis import (
    compute_calibration_confidence,
    get_calibration_confidence,
)
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.services.report_precompute import ReportPrecomputer

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _mongo() -> MongoDBAdapter:
    adapter = object.__new__(MongoDBAdapter)
    adapter._connected = True
    adapter.db = AsyncMongoMockClient()["calibration"]
    return adapter


def _engine(rows: list[dict] | None = None):
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=sa.pool.StaticPool,
        connect_args={"check_same_thread": False},
    )
    table = sa.Table(
        "cio_decisions",
        sa.MetaData(),
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("strategy_id", sa.String(128)),
        sa.Column("timestamp", sa.DateTime),
        sa.Column("action", sa.String(20)),
        sa.Column("confidence", sa.Numeric(6, 5)),
    )
    table.create(engine)
    if rows:
        with engine.begin() as connection:
            connection.execute(table.insert(), rows)
    return engine


def _row(decision_id, action="buy", confidence=Decimal("0.71234"), strategy="s1"):
    return {
        "decision_id": decision_id,
        "strategy_id": strategy,
        "timestamp": datetime(2026, 10, 1),
        "action": action,
        "confidence": confidence,
    }


def _fills(decision_id, start, *, close=True, strategy="s1"):
    rows = [
        {
            "event_type": "filled",
            "strategy_id": strategy,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "fill_qty": "1",
            "fill_price": "100",
            "fee": "0",
            "decision_id": decision_id,
            "fill_time": start,
            "timestamp": start,
            "position_side": "LONG",
        }
    ]
    if close:
        rows.append(
            {
                **rows[0],
                "side": "SELL",
                "fill_price": "110",
                "fill_time": start + timedelta(minutes=5),
                "timestamp": start + timedelta(minutes=5),
            }
        )
    return rows


async def _seed(mongo, fills, decisions=()):
    await mongo.db["execution_events"].insert_many(fills)
    if decisions:
        await mongo.db["cio_decisions"].insert_many(list(decisions))


def _mongo_decision(decision_id, action="buy", confidence=0.9, strategy="s1"):
    return {
        "decision_id": decision_id,
        "strategy_id": strategy,
        "action": action,
        "confidence": confidence,
        "timestamp": T0,
    }


def _record_queries(engine) -> list[list[str]]:
    """The decision ids of every SELECT sent to the engine."""
    seen: list[list[str]] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        seen.append([str(p) for p in parameters])

    return seen


def test_the_executed_action_rule_is_one_set_and_case_insensitive():
    assert calibration.EXECUTED_DECISION_ACTIONS == {"execute", "buy", "sell"}
    for action in ("execute", "buy", "sell", "BUY", "Sell", "EXECUTE"):
        assert calibration.is_executed_decision({"action": action})
    for action in ("hold", "skip", "pause_strategy", "", None):
        assert not calibration.is_executed_decision({"action": action})
    assert calibration._executed_action_filter() == {
        "$regex": "^(buy|execute|sell)$",
        "$options": "i",
    }


@pytest.mark.asyncio
async def test_a_decision_only_in_mysql_gives_a_record_with_its_confidence():
    mongo = _mongo()
    await _seed(mongo, _fills("d-old", T0))
    report = await calibration.get_calibration_records(
        mongo, mysql_adapter=SimpleNamespace(engine=_engine([_row("d-old")]))
    )
    assert [r["decision_id"] for r in report["records"]] == ["d-old"]
    assert report["records"][0]["confidence"] == Decimal("0.71234")
    assert report["skipped"] == 0
    assert "history_unavailable" not in report


@pytest.mark.asyncio
async def test_mongo_overrides_mysql_for_the_same_decision_and_is_not_looked_up():
    mongo = _mongo()
    engine = _engine([_row("d1", confidence=Decimal("0.10000"))])
    queries = _record_queries(engine)
    await _seed(mongo, _fills("d1", T0), [_mongo_decision("d1", confidence=0.9)])
    report = await calibration.get_calibration_records(
        mongo, mysql_adapter=SimpleNamespace(engine=engine)
    )
    assert report["records"][0]["confidence"] == Decimal("0.9")
    assert queries == []  # nothing was missing from Mongo, so MySQL was not asked


@pytest.mark.asyncio
async def test_a_mongo_decision_stored_in_upper_case_is_found_by_the_filter():
    mongo = _mongo()
    await _seed(mongo, _fills("d1", T0), [_mongo_decision("d1", action="BUY")])
    report = await calibration.get_calibration_records(mongo)
    assert len(report["records"]) == 1


@pytest.mark.asyncio
async def test_each_skip_reason_is_reported():
    mongo = _mongo()
    fills = (
        _fills("no-history", T0)
        + _fills("held", T0 + timedelta(hours=1))
        + _fills("bad", T0 + timedelta(hours=2))
        + _fills("none", T0 + timedelta(hours=3))
        + _fills("good", T0 + timedelta(hours=4))
    )
    no_id = _fills("", T0 + timedelta(hours=5))
    for row in no_id:
        row.pop("decision_id")
    engine = _engine(
        [
            _row("held", action="hold"),
            _row("bad", confidence=Decimal("1.50000")),
            _row("none", confidence=None),
            _row("good"),
        ]
    )
    await _seed(mongo, fills + no_id)
    report = await calibration.get_calibration_records(
        mongo, mysql_adapter=SimpleNamespace(engine=engine)
    )
    assert [r["decision_id"] for r in report["records"]] == ["good"]
    assert report["skipped_reasons"] == {
        "decision_not_in_history": 1,
        "invalid_confidence": 1,
        "missing_confidence": 1,
        "entry_decision_unavailable": 1,
        "non_execute_decision": 1,
    }
    assert report["skipped"] == 5


@pytest.mark.asyncio
async def test_no_matching_decision_stays_the_reason_when_history_cannot_be_read():
    mongo = _mongo()
    await _seed(mongo, _fills("d1", T0))
    report = await calibration.get_calibration_records(mongo, mysql_adapter=None)
    assert report["skipped_reasons"] == {"no_matching_cio_decision": 1}
    assert report["history_unavailable"] is True


@pytest.mark.asyncio
async def test_only_the_decisions_of_closed_rounds_within_since_are_looked_up():
    mongo = _mongo()
    engine = _engine([_row(d) for d in ("closed-new", "closed-old", "still-open")])
    queries = _record_queries(engine)
    fills = (
        _fills("closed-old", T0)
        + _fills("closed-new", T0 + timedelta(days=10))
        + _fills("still-open", T0 + timedelta(days=11), close=False)
    )
    await _seed(mongo, fills)
    report = await calibration.get_calibration_records(
        mongo,
        since=T0 + timedelta(days=5),
        mysql_adapter=SimpleNamespace(engine=engine),
    )
    assert [r["decision_id"] for r in report["records"]] == ["closed-new"]
    assert queries == [["closed-new"]]


@pytest.mark.asyncio
async def test_lookups_are_batched(monkeypatch):
    mongo = _mongo()
    ids = [f"d{i}" for i in range(5)]
    engine = _engine([_row(d) for d in ids])
    queries = _record_queries(engine)
    await _seed(
        mongo,
        [row for i, d in enumerate(ids) for row in _fills(d, T0 + timedelta(hours=i))],
    )
    monkeypatch.setattr(calibration, "MYSQL_DECISION_BATCH_SIZE", 2)
    report = await calibration.get_calibration_records(
        mongo, mysql_adapter=SimpleNamespace(engine=engine)
    )
    assert len(report["records"]) == 5
    assert [len(q) for q in queries] == [2, 2, 1]  # three queries for five ids


@pytest.mark.asyncio
async def test_the_id_cap_warns_and_keeps_the_newest_rounds(monkeypatch, caplog):
    mongo = _mongo()
    ids = [f"d{i}" for i in range(4)]
    engine = _engine([_row(d) for d in ids])
    queries = _record_queries(engine)
    await _seed(
        mongo,
        [row for i, d in enumerate(ids) for row in _fills(d, T0 + timedelta(hours=i))],
    )
    monkeypatch.setattr(calibration, "MAX_HISTORIC_DECISION_IDS", 2)
    caplog.set_level(
        logging.WARNING, logger="data_manager.services.calibration_service"
    )
    report = await calibration.get_calibration_records(
        mongo, mysql_adapter=SimpleNamespace(engine=engine)
    )
    assert any(
        "decision lookup reached cap: 2" in r.getMessage() for r in caplog.records
    )
    assert sorted(i for q in queries for i in q) == [
        "d2",
        "d3",
    ]  # the two newest rounds
    assert sorted(r["decision_id"] for r in report["records"]) == ["d2", "d3"]
    assert report["skipped_reasons"] == {"history_lookup_capped": 2}


class _BrokenEngine:
    def connect(self):
        raise RuntimeError("Lost connection to MySQL server")


@pytest.mark.asyncio
async def test_a_failing_history_read_is_a_marker_for_an_uncached_caller_and_an_error_for_a_cached_one():
    mongo = _mongo()
    await _seed(mongo, _fills("d1", T0))
    broken = SimpleNamespace(engine=_BrokenEngine())
    degraded = await calibration.get_calibration_records(mongo, mysql_adapter=broken)
    assert degraded["history_unavailable"] is True
    assert degraded["records"] == []
    with pytest.raises(calibration.CalibrationHistoryUnavailable):
        await calibration.get_calibration_records(
            mongo, mysql_adapter=broken, strict_history=True
        )


@pytest.fixture
def served(monkeypatch):
    mongo = _mongo()
    manager = SimpleNamespace(mongodb_adapter=mongo, mysql_adapter=None)
    precomputer = ReportPrecomputer(manager)
    monkeypatch.setattr(api_module, "db_manager", manager, raising=False)
    monkeypatch.setattr(api_module, "report_precomputer", precomputer, raising=False)
    yield mongo, manager, precomputer
    monkeypatch.setattr(api_module, "db_manager", None, raising=False)
    monkeypatch.setattr(api_module, "report_precomputer", None, raising=False)


@pytest.mark.asyncio
async def test_mysql_down_keeps_the_last_good_cache_and_caches_nothing_cold(served):
    mongo, manager, precomputer = served
    await _seed(mongo, _fills("d1", T0), [_mongo_decision("d1")])
    good = await get_calibration_confidence()  # cold route: computed and cached
    assert len(good["records"]) == 1
    # a new round whose decision is only in MySQL, and MySQL goes down
    await mongo.db["execution_events"].insert_many(_fills("d2", T0 + timedelta(days=1)))
    manager.mysql_adapter = SimpleNamespace(engine=_BrokenEngine())
    await precomputer._refresh(
        "calibration",
        lambda: compute_calibration_confidence(
            manager, source="refresh", strict_history=True
        ),
        source="refresh",
    )
    row = await mongo.db["report_cache"].find_one({"_id": "calibration"})
    assert (
        len(row["body"]["records"]) == 1
    )  # the good report was kept, not replaced by a partial one
    assert "history_unavailable" not in row["body"]
    # a cold cache with MySQL down: a clear error, nothing cached
    await mongo.db["report_cache"].delete_many({})
    with pytest.raises(HTTPException) as error:
        await get_calibration_confidence()
    assert error.value.status_code == 503
    assert await mongo.db["report_cache"].find_one({"_id": "calibration"}) is None
    # a filtered (uncached) call gets the degraded marker
    degraded = await get_calibration_confidence(strategy_id="s1")
    assert degraded["history_unavailable"] is True


@pytest.mark.asyncio
async def test_a_decimal_confidence_from_mysql_is_a_bson_float_in_the_cache(served):
    mongo, manager, _precomputer = served
    manager.mysql_adapter = SimpleNamespace(engine=_engine([_row("d1")]))
    await _seed(mongo, _fills("d1", T0))
    report = await get_calibration_confidence()
    assert report["records"][0]["confidence"] == pytest.approx(0.71234)
    row = await mongo.db["report_cache"].find_one({"_id": "calibration"})
    stored = row["body"]["records"][0]
    assert isinstance(stored["confidence"], float)
    assert isinstance(stored["net_pnl"], float)
    BSON.encode({"body": row["body"]})  # the stored document is plain BSON


@pytest.mark.asyncio
async def test_the_production_refresh_loop_keeps_the_last_good_cache_when_mysql_is_down(
    served, monkeypatch
):
    """The wiring in ReportPrecomputer._run (not a test-local lambda) must pass strict_history=True."""
    import asyncio

    import data_manager.api.routes.analysis as analysis
    import data_manager.api.routes.risk as risk
    from data_manager.services import report_precompute

    mongo, manager, precomputer = served
    await _seed(mongo, _fills("d1", T0), [_mongo_decision("d1")])
    assert (
        len((await get_calibration_confidence())["records"]) == 1
    )  # the good cache row
    # a new closed round whose decision is only in MySQL, and MySQL is down
    await mongo.db["execution_events"].insert_many(_fills("d2", T0 + timedelta(days=1)))
    manager.mysql_adapter = SimpleNamespace(engine=_BrokenEngine())
    # the other reports are not under test
    for module, name in (
        (analysis, "compute_slippage_by_regime"),
        (analysis, "compute_closed_rounds"),
        (risk, "compute_risk_inputs"),
    ):
        monkeypatch.setattr(module, name, AsyncMock(return_value={}))
    precomputer.running = True

    # The loop cancels an unfinished calibration task when it exits, so let it run until the refresh is done.
    real_sleep = asyncio.sleep
    refreshed: list[str] = []
    original_refresh = precomputer._refresh

    async def tracked_refresh(report, callback, **params):
        await original_refresh(report, callback, **params)
        refreshed.append(report)

    precomputer._refresh = tracked_refresh
    ticks = 0

    async def sleep_until_the_calibration_refresh_is_done(_seconds):
        nonlocal ticks
        ticks += 1
        if "calibration" in refreshed or ticks > 500:
            precomputer.running = False
        await real_sleep(0.005)

    monkeypatch.setattr(
        report_precompute.asyncio, "sleep", sleep_until_the_calibration_refresh_is_done
    )
    await precomputer._run()
    assert "calibration" in refreshed  # the production loop really ran the refresh

    row = await mongo.db["report_cache"].find_one({"_id": "calibration"})
    assert (
        len(row["body"]["records"]) == 1
    )  # not replaced by a report missing the MySQL decisions
    assert "history_unavailable" not in row["body"]
