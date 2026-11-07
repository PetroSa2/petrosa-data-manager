"""The path CIO's keep/kill reads, through the app, follows the stats-source switch (petrosa-data-manager#842).

Two routers declare ``/strategy-net-r``; the scorecard one is registered first and serves it, so the switch is
tested on the HTTP path, not on a handler function.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
import data_manager.api.routes.analysis as analysis
import data_manager.api.routes.scorecard as scorecard
from data_manager.services import strategy_stats_source as src


def _snapshot():
    return {
        "computed_at": datetime.now(UTC),
        "rule": "A",
        "strategies": {
            "iceberg_detector": {
                "net_r": {
                    "net_r": ["-1.0", "0.5"],
                    "closed_rounds": 25,
                    "rounds_without_risk": 23,
                }
            }
        },
    }


@pytest.fixture
def http(monkeypatch):
    src.reset_state()
    monkeypatch.setattr(api_module, "db_manager", MagicMock())
    monkeypatch.setattr(
        analysis, "_ground_truth_snapshot", AsyncMock(return_value=_snapshot())
    )
    yield TestClient(api_module.create_app()), monkeypatch
    src.reset_state()


def _switch(monkeypatch, value):
    async def get_source(_db, clock=None):
        return value

    monkeypatch.setattr(src, "get_source", get_source)


@pytest.mark.parametrize("prefix", ["/api/v1/analysis", "/analysis"])
def test_ground_truth_reaches_the_served_path(http, prefix):
    client, monkeypatch = http
    _switch(monkeypatch, "ground_truth")
    netting = AsyncMock(side_effect=AssertionError("netting must not be read"))
    monkeypatch.setattr(scorecard, "_scored", netting)

    body = client.get(f"{prefix}/strategy-net-r").json()

    assert body["metadata"]["stats_source"] == "ground_truth"
    assert body["strategies"]["iceberg_detector"]["rounds_without_risk"] == 23
    netting.assert_not_awaited()


def test_netting_keeps_the_existing_served_response(http):
    client, monkeypatch = http
    _switch(monkeypatch, "netting")
    data = MagicMock(status="incomplete", source_errors={"cio_decisions": ""})
    monkeypatch.setattr(scorecard, "_scored", AsyncMock(return_value=data))

    body = client.get("/api/v1/analysis/strategy-net-r").json()

    assert body == {
        "status": "incomplete",
        "strategies": {},
        "source_errors": {"cio_decisions": ""},
    }
