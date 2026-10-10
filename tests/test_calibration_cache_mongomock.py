"""The calibration report end to end on a real adapter over mongomock (petrosa-data-manager#569).

Cold route -> warm route through the real ReportPrecomputer, the body checked against the CIO's response
contract, the read cap, the ``source`` label of the stage metric, filtered versus unfiltered equivalence with
the read before the precompute, and the order of fills with an equal ``fill_time``.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.encoders import jsonable_encoder
from mongomock_motor import AsyncMongoMockClient
from prometheus_client import REGISTRY

import data_manager.api.app as api_module
import data_manager.services.calibration_service as calibration
from data_manager.api.routes.analysis import (
    compute_calibration_confidence,
    get_calibration_confidence,
)
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.services.report_precompute import ReportPrecomputer


def _adapter() -> MongoDBAdapter:
    adapter = object.__new__(MongoDBAdapter)
    adapter._connected = True
    adapter.db = AsyncMongoMockClient()["calibration"]
    return adapter


def _fill(
    strategy, did, side, price, fill_time, *, position_side=None, ts=None, **extra
):
    row = {
        "event_type": "filled",
        "strategy_id": strategy,
        "symbol": "BTCUSDT",
        "side": side,
        "fill_qty": "0.001",
        "fill_price": price,
        "fee": "0.01",
        "decision_id": did,
        "fill_time": fill_time,
        "timestamp": ts or fill_time,
    }
    if position_side:
        row["position_side"] = position_side
    row.update(extra)
    return row


def _decision(strategy, did, when, confidence=0.5):
    return {
        "decision_id": did,
        "strategy_id": strategy,
        "action": "execute",
        "confidence": confidence,
        "timestamp": when,
    }


def _cio_records_from_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The CIO's response contract (petrosa-cio core/confidence_calibration.py::_records_from_response)."""
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("calibration response records must be a list")
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("calibration records must be objects")
        if not isinstance(record.get("strategy_id"), str):
            raise ValueError("calibration record strategy_id must be a string")
        if not isinstance(record.get("confidence"), int | float):
            raise ValueError("calibration record confidence must be numeric")
        has_net = isinstance(record.get("net_pnl"), int | float)
        has_gross_and_costs = all(
            isinstance(record.get(field), int | float)
            for field in ("gross_pnl", "costs")
        )
        if not has_net and not has_gross_and_costs:
            raise ValueError(
                "calibration record requires net_pnl or gross_pnl and costs"
            )
    return records


async def _seed_round_trips(adapter, count=40):
    now = datetime.now(UTC)
    events, decisions = [], []
    for i in range(count):
        start = now - timedelta(hours=count - i)
        did = f"d{i}"
        decisions.append(_decision("s1", did, start, confidence=0.1 * (i % 9 + 1)))
        events += [
            _fill(
                "s1",
                did,
                side,
                price,
                start + timedelta(minutes=minute),
                position_side="LONG",
            )
            for side, price, minute in (("BUY", "65000.1", 0), ("SELL", "65010.3", 3))
        ]
        events.append(
            {"event_type": "order_submitted", "strategy_id": "s1", "timestamp": start}
        )
    await adapter.db["execution_events"].insert_many(events)
    await adapter.db["cio_decisions"].insert_many(decisions)


def _stage_count(stage: str, source: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "data_manager_report_stage_seconds_count",
            {"report": "calibration", "stage": stage, "source": source},
        )
        or 0.0
    )


@pytest.fixture
def served(monkeypatch):
    """A real adapter over mongomock behind the real route and the real ReportPrecomputer."""
    adapter = _adapter()
    manager = SimpleNamespace(mongodb_adapter=adapter)
    precomputer = ReportPrecomputer(manager)
    monkeypatch.setattr(api_module, "db_manager", manager, raising=False)
    monkeypatch.setattr(api_module, "report_precomputer", precomputer, raising=False)
    yield adapter, manager, precomputer
    monkeypatch.setattr(api_module, "db_manager", None, raising=False)
    monkeypatch.setattr(api_module, "report_precomputer", None, raising=False)


@pytest.mark.asyncio
async def test_cold_route_then_warm_route_serve_a_body_the_cio_accepts(served):
    adapter, _manager, _precomputer = served
    await _seed_round_trips(adapter)

    cold = await get_calibration_confidence()
    row = await adapter.db["report_cache"].find_one({"_id": "calibration"})
    warm = await get_calibration_confidence()

    assert row is not None and row["_id"] == "calibration"
    payload = json.loads(json.dumps(jsonable_encoder(warm)))  # what goes over HTTP
    records = _cio_records_from_response(payload)
    assert len(records) == 40
    assert all(isinstance(r["net_pnl"], float) for r in records)
    assert {r["strategy_id"] for r in records} == {"s1"}
    assert payload["metadata"]["stale"] is False
    assert payload["metadata"]["computed_at"] == payload["metadata"]["calculated_at"]
    assert payload["metadata"]["age_seconds"] >= 0
    # the cold call computed and stored it, the warm call is the cached row
    assert len(cold["records"]) == len(warm["records"]) == 40
    assert warm["metadata"]["computed_at"] == (
        row["computed_at"].replace(tzinfo=UTC).isoformat()
        if row["computed_at"].tzinfo is None
        else row["computed_at"].isoformat()
    )


@pytest.mark.asyncio
async def test_a_second_warm_call_does_not_recompute(served, monkeypatch):
    adapter, manager, _precomputer = served
    await _seed_round_trips(adapter, count=5)
    await get_calibration_confidence()

    async def boom(*_args, **_kwargs):
        raise AssertionError("recomputed on a warm route")

    monkeypatch.setattr(
        "data_manager.api.routes.analysis.compute_calibration_confidence", boom
    )
    warm = await get_calibration_confidence()
    assert len(warm["records"]) == 5


@pytest.mark.asyncio
async def test_the_read_cap_logs_a_warning_for_both_collections(monkeypatch, caplog):
    adapter = _adapter()
    await _seed_round_trips(adapter, count=10)
    monkeypatch.setattr(calibration, "MAX_ROWS", 10)
    caplog.set_level(
        logging.WARNING, logger="data_manager.services.calibration_service"
    )

    await calibration.get_calibration_records(adapter)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("execution_events read reached cap: 10" in m for m in messages)
    assert any("cio_decisions read reached cap: 10" in m for m in messages)


@pytest.mark.asyncio
async def test_under_the_cap_logs_no_warning(monkeypatch, caplog):
    adapter = _adapter()
    await _seed_round_trips(adapter, count=3)
    caplog.set_level(
        logging.WARNING, logger="data_manager.services.calibration_service"
    )
    await calibration.get_calibration_records(adapter)
    assert not [r for r in caplog.records if "reached cap" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_cap_keeps_the_newest_rows(monkeypatch):
    adapter = _adapter()
    await _seed_round_trips(
        adapter, count=20
    )  # 20 decisions d0 (oldest) .. d19 (newest)
    monkeypatch.setattr(calibration, "MAX_ROWS", 10)  # 10 decisions, 10 of the 40 fills
    report = await calibration.get_calibration_records(adapter)
    ids = [r["decision_id"] for r in report["records"]]
    assert ids and ids == sorted(ids, key=lambda d: int(d[1:]))
    assert all(int(d[1:]) >= 15 for d in ids)  # only the newest rounds survive the cut


@pytest.mark.asyncio
async def test_the_stage_metric_carries_the_source_label(served):
    adapter, manager, precomputer = served
    await _seed_round_trips(adapter, count=3)
    before = {
        (stage, source): _stage_count(stage, source)
        for stage in ("read+decode", "compute")
        for source in ("on_demand", "refresh")
    }

    await get_calibration_confidence()  # cold route: on demand
    await precomputer._refresh(
        "calibration",
        lambda: compute_calibration_confidence(manager, source="refresh"),
        source="refresh",
    )

    for stage in ("read+decode", "compute"):
        assert _stage_count(stage, "on_demand") == before[(stage, "on_demand")] + 1
        assert _stage_count(stage, "refresh") == before[(stage, "refresh")] + 1


async def _read_before_the_precompute(mongodb, *, since=None, strategy_id=None):
    """The read as it was before #569: oldest first, every event type, no event_type filter."""
    filters = {"strategy_id": strategy_id} if strategy_id else None
    events = await mongodb.find_filtered(
        "execution_events", filters=filters, limit=calibration.MAX_ROWS, sort_order=1
    )
    decisions = await mongodb.find_filtered(
        "cio_decisions",
        filters={"strategy_id": strategy_id, "action": "execute"},
        limit=calibration.MAX_ROWS,
        sort_order=1,
    )
    return calibration.build_calibration_records(events, decisions, since=since)


def _key(report):
    return sorted(
        (r["decision_id"], r["strategy_id"], round(r["net_pnl"], 9), r["closed_at"])
        for r in report["records"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(4))
async def test_filtered_and_unfiltered_reads_match_the_read_before_the_precompute(seed):
    rnd = random.Random(seed)
    adapter = _adapter()
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    events, decisions = [], []
    for i in range(150):
        strategy = rnd.choice(["s1", "s2", "s3"])
        symbol = rnd.choice(["BTCUSDT", "ETHUSDT"])
        did = f"d{i}"
        start = t0 + timedelta(minutes=10 * i)
        decisions.append(_decision(strategy, did, start, rnd.random()))
        position_side = rnd.choice(["LONG", "SHORT", None])
        opening = "buy" if position_side != "SHORT" else "sell"
        closing = "sell" if opening == "buy" else "buy"
        for step, (side, kind) in enumerate(
            [(opening, "filled"), (closing, rnd.choice(["filled", "partial_fill"]))]
        ):
            when = start + timedelta(minutes=step * rnd.randint(1, 5))
            events.append(
                _fill(
                    strategy,
                    did,
                    side,
                    str(100 + rnd.random()),
                    when,
                    position_side=position_side,
                    event_type=kind,
                    symbol=symbol,
                    fill_qty="1",
                )
            )
        for kind in ("order_submitted", "order_accepted", "canceled", "rejected"):
            events.append(
                {
                    "event_type": kind,
                    "strategy_id": strategy,
                    "symbol": symbol,
                    "side": "buy",
                    "fill_qty": "1",
                    "fill_price": "1",
                    "decision_id": did,
                    "timestamp": start,
                }
            )
    await adapter.db["execution_events"].insert_many(events)
    await adapter.db["cio_decisions"].insert_many(decisions)

    for strategy_id in (None, "s2"):
        before = await _read_before_the_precompute(adapter, strategy_id=strategy_id)
        after = await calibration.get_calibration_records(
            adapter, strategy_id=strategy_id
        )
        assert before["records"], "the scenario must produce records"
        assert _key(after) == _key(before)
        assert after["skipped"] == before["skipped"]


@pytest.mark.asyncio
async def test_fills_with_an_equal_fill_time_keep_their_ingestion_order():
    """A close and a reopen in the same ms on a NET leg: each PnL goes to its own decision."""
    adapter = _adapter()
    t = datetime(2026, 10, 1, tzinfo=UTC)
    one_minute = t + timedelta(minutes=1)
    no_fee = {"fill_qty": "1", "fee": "0"}
    events = [
        _fill("s1", "d1", "buy", "100", t, ts=t, **no_fee),
        # the close of d1 and the reopen of d2 share one fill_time; ingestion differs by 1 ms
        _fill(
            "s1",
            "d1",
            "sell",
            "110",
            one_minute,
            ts=one_minute + timedelta(milliseconds=1),
            **no_fee,
        ),
        _fill(
            "s1",
            "d2",
            "buy",
            "110",
            one_minute,
            ts=one_minute + timedelta(milliseconds=2),
            **no_fee,
        ),
        _fill("s1", "d2", "sell", "130", t + timedelta(minutes=2), **no_fee),
    ]
    await adapter.db["execution_events"].insert_many(events)
    await adapter.db["cio_decisions"].insert_many(
        [_decision("s1", d, t) for d in ("d1", "d2")]
    )

    report = await calibration.get_calibration_records(adapter)
    before = await _read_before_the_precompute(adapter)

    by_decision = {r["decision_id"]: float(r["net_pnl"]) for r in report["records"]}
    assert by_decision == {"d1": pytest.approx(10.0), "d2": pytest.approx(20.0)}
    assert _key(report) == _key(before)


@pytest.mark.asyncio
async def test_equal_timestamps_keep_identity_order_and_newest_decision_wins():
    adapter = _adapter()
    t = datetime(2026, 10, 1, tzinfo=UTC)
    events = [
        _fill("s1", "d1", "buy", "100", t, ts=t, fill_qty="1", fee="0"),
        _fill("s1", "d1", "sell", "110", t, ts=t, fill_qty="1", fee="0"),
    ]
    await adapter.db["execution_events"].insert_many(events)
    await adapter.db["cio_decisions"].insert_many(
        [
            _decision("s1", "d1", t, confidence=0.1),
            _decision("s1", "d1", t, confidence=0.9),
        ]
    )

    newest_first = await adapter.find_filtered(
        "execution_events",
        filters={"strategy_id": "s1"},
        limit=10,
        sort_order=-1,
        secondary_sort_field="_id",
    )
    assert [row["side"] for row in newest_first] == ["sell", "buy"]
    assert [row["side"] for row in reversed(newest_first)] == ["buy", "sell"]

    report = await calibration.get_calibration_records(adapter)

    assert report["records"][0]["decision_id"] == "d1"
    assert float(report["records"][0]["confidence"]) == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_missing_mongo_decision_is_joined_from_historic_mysql(monkeypatch):
    adapter = _adapter()
    t = datetime(2026, 10, 1, tzinfo=UTC)
    await adapter.db["execution_events"].insert_many(
        [
            _fill("s1", "historic", "buy", "100", t),
            _fill("s1", "historic", "sell", "110", t + timedelta(minutes=1)),
        ]
    )

    def historic(_decision_ids, mysql_uri):
        assert mysql_uri == "mysql://history"
        return [_decision("s1", "historic", t, confidence=0.7)], True

    monkeypatch.setattr(calibration, "_read_historic_decisions", historic)
    report = await calibration.get_calibration_records(
        adapter, mysql_uri="mysql://history"
    )

    assert report["skipped"] == 0
    assert report["records"][0]["decision_id"] == "historic"
    assert float(report["records"][0]["confidence"]) == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_history_checked_missing_decision_gets_distinct_skip_reason(monkeypatch):
    adapter = _adapter()
    t = datetime(2026, 10, 1, tzinfo=UTC)
    await adapter.db["execution_events"].insert_many(
        [
            _fill("s1", "missing", "buy", "100", t),
            _fill("s1", "missing", "sell", "110", t + timedelta(minutes=1)),
        ]
    )
    monkeypatch.setattr(
        calibration,
        "_read_historic_decisions",
        lambda _decision_ids, _mysql_uri: ([], True),
    )

    report = await calibration.get_calibration_records(
        adapter,
        mysql_uri="mysql://history",
    )

    assert report["skipped"] == 1
    assert report["skipped_reasons"] == {"decision_not_in_history": 1}


def test_historic_decisions_use_the_read_only_role_factory(monkeypatch):
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    table = sa.Table(
        "cio_decisions",
        sa.MetaData(),
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("strategy_id", sa.String(128), nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("action", sa.String(20)),
        sa.Column("confidence", sa.Numeric(6, 5)),
    )
    table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            {
                "decision_id": "historic",
                "strategy_id": "s1",
                "timestamp": datetime(2026, 10, 1),
                "action": "execute",
                "confidence": 0.7,
            },
        )
    roles = []
    monkeypatch.setattr(
        calibration,
        "create_read_only_engine",
        lambda uri, role: roles.append((uri, role)) or engine,
    )
    monkeypatch.setattr(calibration, "mark_engine_closing", lambda candidate: None)

    rows, checked = calibration._read_historic_decisions(
        {"historic"}, "mysql://history"
    )

    assert checked is True
    assert rows[0]["decision_id"] == "historic"
    assert roles == [("mysql://history", "adhoc")]


def test_the_builder_sorts_equal_fill_times_stably_in_the_order_it_is_given():
    """The service hands the builder oldest-first rows; the builder must keep that order on ties."""
    t = datetime(2026, 10, 1, tzinfo=UTC)
    no_fee = {"fill_qty": "1", "fee": "0"}
    events = [
        _fill("s1", "d1", "buy", "100", t, **no_fee),
        _fill("s1", "d1", "sell", "110", t + timedelta(minutes=1), **no_fee),
        _fill("s1", "d2", "buy", "110", t + timedelta(minutes=1), **no_fee),
        _fill("s1", "d2", "sell", "130", t + timedelta(minutes=2), **no_fee),
    ]
    decisions = [_decision("s1", d, t) for d in ("d1", "d2")]
    ascending = calibration.build_calibration_records(events, decisions)
    descending = calibration.build_calibration_records(
        list(reversed(events)), decisions
    )
    assert {r["decision_id"] for r in ascending["records"]} == {"d1", "d2"}
    # newest-first input on a tie credits the wrong decision: that is why the service reverses the read
    assert _key(ascending) != _key(descending)
