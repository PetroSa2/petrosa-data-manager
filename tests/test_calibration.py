from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from bson import BSON
from fastapi import HTTPException

import data_manager.api.app as api_module
import data_manager.services.calibration_service as calibration
from data_manager.api.app import create_app
from data_manager.api.middleware.metrics import REQUEST_DURATION
from data_manager.api.routes.analysis import (
    get_calibration_confidence,
    get_latest_calibration_report,
)
from data_manager.api.routes.health import calibration_health
from data_manager.services.calibration_service import (
    build_calibration_records,
    calibration_freshness,
)
from data_manager.services.report_precompute import ReportPrecomputer


def _fill(side: str, timestamp: str, decision_id: str) -> dict:
    return {
        "event_type": "filled",
        "strategy_id": "alpha",
        "symbol": "BTCUSDT",
        "side": side,
        "fill_qty": "1",
        "fill_price": "101" if side == "sell" else "100",
        "fee": "0.10",
        "fee_asset": "USDT",
        "decision_id": decision_id,
        "fill_time": timestamp,
    }


def test_build_calibration_records_joins_and_preserves_decimal_values() -> None:
    result = build_calibration_records(
        [
            _fill("buy", "2026-10-01T12:00:00Z", "d1"),
            _fill("sell", "2026-10-01T12:01:00Z", "d1"),
        ],
        [
            {
                "decision_id": "d1",
                "strategy_id": "alpha",
                "action": "execute",
                "confidence": "0.875",
            }
        ],
    )

    record = result["records"][0]
    assert record["confidence"] == Decimal("0.875")
    assert record["gross_pnl"] == Decimal("1")
    assert record["costs"] == Decimal("0.20")
    assert record["net_pnl"] == Decimal("0.80")
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_calibration_report_is_bson_safe_when_cached() -> None:
    class Collection:
        def __init__(self) -> None:
            self.document = None

        async def replace_one(self, query, document, upsert=False) -> None:
            self.document = document

    collection = Collection()
    manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"report_cache": collection})
    )
    body = build_calibration_records(
        [
            _fill("buy", "2026-10-01T12:00:00Z", "d1"),
            _fill("sell", "2026-10-01T12:01:00Z", "d1"),
        ],
        [{"decision_id": "d1", "action": "execute", "confidence": "0.875"}],
    )

    await ReportPrecomputer(manager)._put("calibration", body)

    assert collection.document is not None
    BSON.encode(collection.document["body"])
    record = collection.document["body"]["records"][0]
    assert isinstance(record["confidence"], float)
    assert isinstance(record["net_pnl"], float)


def test_build_calibration_records_skips_missing_invalid_and_non_execute_decisions() -> (
    None
):
    rows = [
        _fill("buy", "2026-10-01T12:00:00Z", "missing"),
        _fill("sell", "2026-10-01T12:01:00Z", "missing"),
        _fill("buy", "2026-10-01T13:00:00Z", "d2"),
        _fill("sell", "2026-10-01T13:01:00Z", "d2"),
    ]
    result = build_calibration_records(
        rows,
        [{"decision_id": "d2", "action": "pause", "confidence": "0.9"}],
    )

    assert result == {
        "records": [],
        "skipped": 2,
        "skipped_reasons": {
            "no_matching_cio_decision": 1,
            "non_execute_decision": 1,
        },
    }


def test_build_calibration_records_distinguishes_missing_history() -> None:
    rows = [
        _fill("buy", "2026-10-01T12:00:00Z", "missing"),
        _fill("sell", "2026-10-01T12:01:00Z", "missing"),
    ]

    result = build_calibration_records(rows, [], history_checked=True)

    assert result["skipped_reasons"] == {"decision_not_in_history": 1}


def test_historic_decisions_use_batched_read_only_role_factory(monkeypatch) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
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
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {
                    "decision_id": "d1",
                    "strategy_id": "alpha",
                    "timestamp": datetime(2026, 10, 1),
                    "action": "execute",
                    "confidence": 0.7,
                },
                {
                    "decision_id": "d2",
                    "strategy_id": "alpha",
                    "timestamp": datetime(2026, 10, 2),
                    "action": "execute",
                    "confidence": 0.8,
                },
            ],
        )

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        calibration,
        "create_read_only_engine",
        lambda uri, role: calls.append((uri, role)) or engine,
    )
    monkeypatch.setattr(calibration, "mark_engine_closing", lambda candidate: None)
    monkeypatch.setattr(calibration, "MYSQL_DECISION_BATCH_SIZE", 1)

    rows, checked = calibration._read_historic_decisions(
        {"d1", "d2"}, "mysql://history"
    )

    assert checked is True
    assert {row["decision_id"] for row in rows} == {"d1", "d2"}
    assert calls == [("mysql://history", "adhoc")]


def test_build_calibration_records_uses_later_valid_executed_confidence() -> None:
    rows = [
        _fill("buy", "2026-10-01T12:00:00Z", "d1"),
        _fill("sell", "2026-10-01T12:01:00Z", "d2"),
    ]
    result = build_calibration_records(
        rows,
        [
            {"decision_id": "d1", "action": "execute", "confidence": None},
            {"decision_id": "d2", "action": "execute", "confidence": "0.7"},
        ],
    )

    assert result["records"][0]["confidence"] == Decimal("0.7")


def test_build_calibration_records_classifies_missing_and_invalid_confidence() -> None:
    rows = [
        _fill("buy", "2026-10-01T12:00:00Z", None),
        _fill("sell", "2026-10-01T12:01:00Z", None),
        _fill("buy", "2026-10-01T13:00:00Z", "invalid"),
        _fill("sell", "2026-10-01T13:01:00Z", "invalid"),
        _fill("buy", "2026-10-01T14:00:00Z", "missing"),
        _fill("sell", "2026-10-01T14:01:00Z", "missing"),
    ]

    result = build_calibration_records(
        rows,
        [
            {"decision_id": "invalid", "action": "execute", "confidence": 2},
            {"decision_id": "missing", "action": "execute", "confidence": None},
        ],
    )

    assert result["skipped"] == 3
    assert result["skipped_reasons"] == {
        "invalid_confidence": 1,
        "missing_confidence": 1,
        "no_decision_id_on_fill": 1,
    }


def test_build_calibration_records_applies_since() -> None:
    rows = [
        _fill("buy", "2026-10-01T12:00:00Z", "d1"),
        _fill("sell", "2026-10-01T12:01:00Z", "d1"),
    ]
    result = build_calibration_records(
        rows,
        [{"decision_id": "d1", "action": "execute", "confidence": 0.5}],
        since=datetime(2026, 10, 2, tzinfo=UTC),
    )

    assert result == {"records": [], "skipped": 0, "skipped_reasons": {}}


def test_calibration_route_is_mirrored_under_both_analysis_prefixes() -> None:
    paths = set(create_app().openapi()["paths"])

    assert "/analysis/calibration/confidence" in paths
    assert "/api/v1/analysis/calibration/confidence" in paths
    assert "/api/v1/calibration/latest" in paths


def test_calibration_freshness_requires_recent_report() -> None:
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    recent = [{"closed_at": "2026-10-09T11:45:00+00:00"}]
    stale = [{"closed_at": "2026-10-09T11:29:59+00:00"}]

    assert calibration_freshness(recent, now=now)["fresh"] is True
    assert calibration_freshness(stale, now=now)["fresh"] is False
    assert calibration_freshness([], now=now)["fresh"] is False
    assert (
        calibration_freshness([{"closed_at": "2026-10-09T11:45:00"}], now=now)["fresh"]
        is True
    )


@pytest.mark.asyncio
async def test_calibration_route_reads_gateway_and_filters() -> None:
    calls: list[tuple[str, dict]] = []

    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            calls.append((collection, kwargs))
            return []

    api_module.db_manager = SimpleNamespace(mongodb_adapter=Mongo())
    try:
        result = await get_calibration_confidence(
            since=datetime(2026, 10, 1, tzinfo=UTC), strategy_id="alpha"
        )
    finally:
        api_module.db_manager = None

    assert result == {"records": [], "skipped": 0, "skipped_reasons": {}}
    assert calls[0][1]["filters"] == {
        "event_type": {"$in": ["filled", "partial_fill"]},
        "strategy_id": "alpha",
    }
    assert calls[1][1]["filters"] == {"strategy_id": "alpha", "action": "execute"}


@pytest.mark.asyncio
async def test_calibration_route_requires_database() -> None:
    api_module.db_manager = None
    with pytest.raises(HTTPException) as error:
        await get_calibration_confidence()
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_default_calibration_route_serves_cached_report() -> None:
    precomputer = SimpleNamespace(
        get_or_compute=AsyncMock(
            return_value={"records": [], "skipped": 0, "skipped_reasons": {}}
        )
    )
    api_module.db_manager = SimpleNamespace(mongodb_adapter=object())
    api_module.report_precomputer = precomputer
    try:
        result = await get_calibration_confidence()
    finally:
        api_module.db_manager = None
        api_module.report_precomputer = None

    assert result == {"records": [], "skipped": 0, "skipped_reasons": {}}
    precomputer.get_or_compute.assert_awaited_once()
    assert precomputer.get_or_compute.await_args.args[0] == "calibration"


@pytest.mark.asyncio
async def test_default_calibration_route_reads_back_real_cached_body() -> None:
    class Collection:
        def __init__(self) -> None:
            self.rows = {}

        async def find_one(self, query):
            return self.rows.get(query["_id"])

        async def replace_one(self, query, document, upsert=False):
            self.rows[query["_id"]] = document

    collection = Collection()
    manager = SimpleNamespace(
        mongodb_adapter=SimpleNamespace(db={"report_cache": collection})
    )
    body = build_calibration_records(
        [
            _fill("buy", "2026-10-01T12:00:00Z", "d1"),
            _fill("sell", "2026-10-01T12:01:00Z", "d1"),
        ],
        [{"decision_id": "d1", "action": "execute", "confidence": "0.875"}],
    )
    await ReportPrecomputer(manager)._put("calibration", body)

    api_module.db_manager = manager
    api_module.report_precomputer = ReportPrecomputer(manager)
    try:
        result = await get_calibration_confidence()
    finally:
        api_module.db_manager = None
        api_module.report_precomputer = None

    record = result["records"][0]
    assert isinstance(record["confidence"], float)
    assert isinstance(record["net_pnl"], float)
    assert record["strategy_id"] == "alpha"


def test_request_duration_has_long_report_buckets() -> None:
    assert 30.0 in REQUEST_DURATION._upper_bounds
    assert 60.0 in REQUEST_DURATION._upper_bounds


@pytest.mark.asyncio
async def test_latest_calibration_route_adds_freshness() -> None:
    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            if collection == "execution_events":
                return []
            return []

    api_module.db_manager = SimpleNamespace(mongodb_adapter=Mongo())
    try:
        result = await get_latest_calibration_report()
    finally:
        api_module.db_manager = None

    assert result["records"] == []
    assert result["fresh"] is False
    assert result["max_age_minutes"] == 30


@pytest.mark.asyncio
async def test_latest_calibration_route_returns_fresh_report(monkeypatch) -> None:
    async def fake_latest(*args, **kwargs):
        return {
            "records": [{"closed_at": "2026-10-09T11:45:00+00:00"}],
            "skipped": 0,
            "latest_at": "2026-10-09T11:45:00+00:00",
            "fresh": True,
            "max_age_minutes": 30,
            "age_minutes": 15.0,
        }

    monkeypatch.setattr(
        "data_manager.services.calibration_service.get_latest_calibration",
        fake_latest,
    )
    api_module.db_manager = SimpleNamespace(mongodb_adapter=object())
    try:
        result = await get_latest_calibration_report()
    finally:
        api_module.db_manager = None

    assert result["fresh"] is True
    assert result["latest_at"] == "2026-10-09T11:45:00+00:00"


@pytest.mark.asyncio
async def test_latest_calibration_route_maps_query_failure_to_503() -> None:
    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            raise RuntimeError("database unavailable")

    api_module.db_manager = SimpleNamespace(mongodb_adapter=Mongo())
    try:
        with pytest.raises(HTTPException) as error:
            await get_latest_calibration_report()
    finally:
        api_module.db_manager = None

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_calibration_health_reports_unavailable_without_database() -> None:
    api_module.db_manager = None
    response = await calibration_health()

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_calibration_health_reports_empty_data_as_degraded() -> None:
    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            return []

    api_module.db_manager = SimpleNamespace(mongodb_adapter=Mongo())
    try:
        response = await calibration_health()
    finally:
        api_module.db_manager = None

    assert response["status"] == "degraded"
    assert response["fresh"] is False


@pytest.mark.asyncio
async def test_calibration_health_reports_query_failure_as_unavailable() -> None:
    class Mongo:
        async def find_filtered(self, collection: str, **kwargs: object) -> list[dict]:
            raise RuntimeError("database unavailable")

    api_module.db_manager = SimpleNamespace(mongodb_adapter=Mongo())
    try:
        response = await calibration_health()
    finally:
        api_module.db_manager = None

    assert response.status_code == 503
