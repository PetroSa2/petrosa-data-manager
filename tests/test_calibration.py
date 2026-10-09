from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes.analysis import (
    get_calibration_confidence,
    get_latest_calibration_report,
)
from data_manager.api.routes.health import calibration_health
from data_manager.services.calibration_service import (
    build_calibration_records,
    calibration_freshness,
)


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

    assert result == {"records": [], "skipped": 2}


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

    assert result == {"records": [], "skipped": 0}


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

    assert result == {"records": [], "skipped": 0}
    assert calls[0][1]["filters"] == {"strategy_id": "alpha"}
    assert calls[1][1]["filters"] == {"strategy_id": "alpha", "action": "execute"}


@pytest.mark.asyncio
async def test_calibration_route_requires_database() -> None:
    api_module.db_manager = None
    with pytest.raises(HTTPException) as error:
        await get_calibration_confidence()
    assert error.value.status_code == 503


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
