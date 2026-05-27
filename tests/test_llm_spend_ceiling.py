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
