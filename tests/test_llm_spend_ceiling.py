"""Integration test for FR63 AC6 — ceiling breach → alert + fallback.

Simulates a ceiling breach by setting a zero-USD ceiling, recording a token
batch, then verifying that `Orchestrator._check_spend_ceiling` fires the alert
and disables `use_llm_reasoning`. Also verifies period-roll recovery (AC5).
"""

from __future__ import annotations

from datetime import UTC, date
from unittest.mock import AsyncMock, patch

import pytest

from data_manager.api.routes.config import AppConfigRequest

# ---------------------------------------------------------------------------
# AC3 — operator-config model carries llm_spend_ceiling_usd_per_day
# ---------------------------------------------------------------------------


def test_app_config_request_ceiling_default():
    """llm_spend_ceiling_usd_per_day defaults to 5.0."""
    req = AppConfigRequest(
        enabled_strategies=["s1"],
        symbols=["BTCUSDT"],
        candle_periods=["1h"],
        changed_by="test",
    )
    assert req.llm_spend_ceiling_usd_per_day == 5.0


def test_app_config_request_ceiling_custom():
    """Operator can set a non-default ceiling."""
    req = AppConfigRequest(
        enabled_strategies=["s1"],
        symbols=["BTCUSDT"],
        candle_periods=["1h"],
        changed_by="test",
        llm_spend_ceiling_usd_per_day=10.0,
    )
    assert req.llm_spend_ceiling_usd_per_day == 10.0


def test_app_config_request_ceiling_zero_allowed():
    """Ceiling may be set to 0 (immediately triggers bypass)."""
    req = AppConfigRequest(
        enabled_strategies=[],
        symbols=[],
        candle_periods=[],
        changed_by="test",
        llm_spend_ceiling_usd_per_day=0.0,
    )
    assert req.llm_spend_ceiling_usd_per_day == 0.0


# ---------------------------------------------------------------------------
# AC3 — /api/v1/config/application HTTP routes carry the ceiling field
# ---------------------------------------------------------------------------


@pytest.fixture
def config_api_client(mock_db_manager):
    """Build an API TestClient with the mock db_manager injected."""
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module

    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    # The config router reads db_manager from its own module too.
    import data_manager.api.routes.config as config_module

    config_module.db_manager = mock_db_manager
    yield TestClient(app)
    api_module.db_manager = None
    config_module.db_manager = None


def test_get_application_config_returns_default_ceiling_when_empty(config_api_client):
    """When no config in MongoDB, /application returns the 5.0 default ceiling
    (covers the default-branch diff line in config.py).
    """
    # mock_db_manager.configuration.get_app_config returns None by default;
    # set it explicitly to be safe.
    from unittest.mock import AsyncMock

    import data_manager.api.routes.config as config_module

    config_module.db_manager.configuration.get_app_config = AsyncMock(return_value=None)

    resp = config_api_client.get("/api/v1/config/application")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["llm_spend_ceiling_usd_per_day"] == 5.0
    assert body["data"]["source"] == "default"


def test_get_application_config_returns_ceiling_from_mongodb(config_api_client):
    """When MongoDB returns a config with a ceiling, it round-trips through the
    response (covers the mongodb-path diff lines).
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    import data_manager.api.routes.config as config_module

    now = datetime.now(UTC)
    config_module.db_manager.configuration.get_app_config = AsyncMock(
        return_value={
            "parameters": {
                "enabled_strategies": ["s1"],
                "symbols": ["BTCUSDT"],
                "candle_periods": ["1h"],
                "min_confidence": 0.6,
                "max_confidence": 0.95,
                "max_positions": 10,
                "position_sizes": [100, 200, 500, 1000],
                "llm_spend_ceiling_usd_per_day": 12.5,
            },
            "version": 7,
            "created_at": now,
            "updated_at": now,
        }
    )

    resp = config_api_client.get("/api/v1/config/application")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["llm_spend_ceiling_usd_per_day"] == 12.5
    assert body["data"]["source"] == "mongodb"
    assert body["data"]["version"] == 7


def test_update_application_config_persists_ceiling_field(config_api_client):
    """POST /application includes the ceiling in the upsert payload
    (covers the upsert-payload diff line in config.py).
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    import data_manager.api.routes.config as config_module

    captured: dict = {}

    async def _fake_upsert(*, parameters, changed_by, reason):
        captured["parameters"] = parameters
        captured["changed_by"] = changed_by
        captured["reason"] = reason
        return {
            "parameters": parameters,
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

    config_module.db_manager.configuration.upsert_app_config = _fake_upsert
    # validate_only=False path requires validate_application_config to pass —
    # the model validation enforces non-negative ceiling, so we send a real
    # value the validator will accept.
    config_module.db_manager.configuration.get_app_config = AsyncMock(return_value=None)

    resp = config_api_client.post(
        "/api/v1/config/application",
        json={
            "enabled_strategies": ["s1"],
            "symbols": ["BTCUSDT"],
            "candle_periods": ["1h"],
            "min_confidence": 0.6,
            "max_confidence": 0.95,
            "max_positions": 10,
            "position_sizes": [100, 200, 500, 1000],
            "llm_spend_ceiling_usd_per_day": 7.25,
            "changed_by": "tester",
            "reason": "raise ceiling for FR63 test",
            "validate_only": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert captured["parameters"]["llm_spend_ceiling_usd_per_day"] == 7.25
    assert captured["changed_by"] == "tester"


# ---------------------------------------------------------------------------
# AC6 — ceiling breach → alert + fallback (integration with CIO orchestrator)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_spend_tracker():
    """Reset the CIO spend tracker singleton before each test."""
    try:
        from cio.core.spend_tracker import LlmSpendTracker

        LlmSpendTracker.instance().reset_for_test()
        yield
        LlmSpendTracker.instance().reset_for_test()
    except ImportError:
        yield


@pytest.mark.asyncio
async def test_ceiling_breach_triggers_alert_and_bypass():
    """AC6: exceeding ceiling disables use_llm_reasoning and fires alert."""
    pytest.importorskip("cio")

    from cio.core.orchestrator import Orchestrator
    from cio.core.spend_tracker import LlmSpendTracker

    tracker = LlmSpendTracker.instance()
    # Force zero ceiling so any spend causes breach
    tracker.reset_for_test(ceiling_usd=0.0)
    tracker.record("PETROSA_PROMPT_ACTION_CLASSIFIER", 10_000, 5_000)

    orch = Orchestrator.__new__(Orchestrator)
    orch.use_llm_reasoning = True
    orch._ceiling_triggered_bypass = False

    with patch(
        "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
        new_callable=AsyncMock,
    ) as mock_alert:
        await orch._check_spend_ceiling("test-corr-id")

    assert not orch.use_llm_reasoning, (
        "LLM reasoning must be disabled on ceiling breach"
    )
    assert orch._ceiling_triggered_bypass
    mock_alert.assert_awaited_once()
    call_kwargs = mock_alert.call_args
    assert "FR63" in str(call_kwargs) or "ceiling" in str(call_kwargs).lower()


@pytest.mark.asyncio
async def test_period_roll_restores_llm_reasoning():
    """AC5: period reset re-enables LLM reasoning after ceiling-triggered bypass."""
    pytest.importorskip("cio")

    from cio.core.orchestrator import Orchestrator
    from cio.core.spend_tracker import LlmSpendTracker, PeriodSpend

    orch = Orchestrator.__new__(Orchestrator)
    orch.use_llm_reasoning = False
    orch._ceiling_triggered_bypass = True

    tracker = LlmSpendTracker.instance()
    # Simulate period roll: set the tracker to yesterday
    import datetime

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    tracker._current = PeriodSpend(
        period_date=yesterday,
        ceiling_usd_per_day=5.0,
    )

    with patch(
        "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
        new_callable=AsyncMock,
    ) as mock_alert:
        await orch._check_spend_ceiling("test-corr-id-recovery")

    assert orch.use_llm_reasoning, "LLM reasoning must be re-enabled on period roll"
    assert not orch._ceiling_triggered_bypass
    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_breach_no_alert():
    """AC6: under-ceiling spend does not trigger alert or bypass."""
    pytest.importorskip("cio")

    from cio.core.orchestrator import Orchestrator
    from cio.core.spend_tracker import LlmSpendTracker

    tracker = LlmSpendTracker.instance()
    tracker.reset_for_test(ceiling_usd=1000.0)  # Very high ceiling
    tracker.record("PETROSA_PROMPT_REGIME_CLASSIFIER", 100, 50)

    orch = Orchestrator.__new__(Orchestrator)
    orch.use_llm_reasoning = True
    orch._ceiling_triggered_bypass = False

    with patch(
        "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
        new_callable=AsyncMock,
    ) as mock_alert:
        await orch._check_spend_ceiling("test-no-breach")

    assert orch.use_llm_reasoning, "LLM reasoning must remain enabled under ceiling"
    mock_alert.assert_not_awaited()
