"""The audited strategy-stats-source endpoint (petrosa-data-manager#842)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
import data_manager.api.routes.config as config_module
from data_manager.services import strategy_stats_source as src

NOW = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)


@pytest.fixture
def client():
    stored = {
        "parameters": {"symbols": ["BTCUSDT"], "max_positions": 10},
        "version": 7,
        "created_at": NOW,
        "updated_at": NOW,
        "changed_by": "earlier",
        "reason": "earlier change",
    }
    configuration = MagicMock()
    configuration.get_app_config = AsyncMock(side_effect=lambda: stored)

    async def upsert(parameters, changed_by, reason=None):
        stored.update(
            parameters=parameters,
            version=stored["version"] + 1,
            changed_by=changed_by,
            reason=reason,
        )
        return dict(stored)

    configuration.upsert_app_config = AsyncMock(side_effect=upsert)
    db = MagicMock()
    db.configuration = configuration
    app = api_module.create_app()
    api_module.db_manager = db
    config_module.db_manager = db
    src.reset_state()
    try:
        yield TestClient(app), configuration, stored
    finally:
        api_module.db_manager = None
        config_module.db_manager = None
        src.reset_state()


def test_flip_is_an_audited_version_with_the_decision_link(client):
    http, configuration, stored = client
    response = http.put(
        "/api/v1/config/strategy-stats-source",
        json={
            "source": "ground_truth",
            "changed_by": "developer session",
            "decision_ref": "https://github.com/PetroSa2/petrosa-data-manager/issues/842",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["version"] == 8
    kwargs = configuration.upsert_app_config.await_args.kwargs
    assert kwargs["parameters"]["strategy_stats_source"] == "ground_truth"
    assert kwargs["parameters"]["symbols"] == ["BTCUSDT"]  # other parameters kept
    assert "issues/842" in kwargs["reason"]
    current = http.get("/api/v1/config/strategy-stats-source").json()["data"]
    assert (
        current["source"] == "ground_truth"
        and current["changed_by"] == "developer session"
    )


def test_flip_back_is_one_call(client):
    http, _, stored = client
    stored["parameters"]["strategy_stats_source"] = "ground_truth"
    response = http.put(
        "/api/v1/config/strategy-stats-source",
        json={
            "source": "netting",
            "changed_by": "operator",
            "decision_ref": "rollback",
        },
    )
    assert response.status_code == 200
    assert stored["parameters"]["strategy_stats_source"] == "netting"


@pytest.mark.parametrize(
    "body",
    [
        {"source": "bogus", "changed_by": "x", "decision_ref": "y"},
        {"source": "netting", "changed_by": "", "decision_ref": "y"},
        {"source": "netting", "changed_by": "x"},
    ],
)
def test_bad_requests_change_nothing(client, body):
    http, configuration, _ = client
    response = http.put("/api/v1/config/strategy-stats-source", json=body)
    assert response.status_code in (400, 422)
    configuration.upsert_app_config.assert_not_awaited()


def test_a_general_config_update_keeps_the_switch(client):
    http, configuration, stored = client
    stored["parameters"]["strategy_stats_source"] = "ground_truth"
    response = http.post(
        "/api/v1/config/application",
        json={
            "enabled_strategies": ["s1"],
            "symbols": ["BTCUSDT"],
            "candle_periods": ["1h"],
            "min_confidence": 0.6,
            "max_confidence": 0.95,
            "max_positions": 10,
            "position_sizes": [100],
            "llm_spend_ceiling_usd_per_day": 5.0,
            "changed_by": "someone",
        },
    )
    assert response.status_code == 200
    parameters = configuration.upsert_app_config.await_args.kwargs["parameters"]
    assert parameters["strategy_stats_source"] == "ground_truth"
