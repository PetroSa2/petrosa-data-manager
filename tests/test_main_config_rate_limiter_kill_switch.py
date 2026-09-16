"""
Regression tests for the `CONFIG_RATE_LIMIT_ENABLED` kill-switch on
`DataManagerApp._run_api_server`'s `ConfigRateLimiter` wiring.

data-manager#302: live Atlas re-query found the `config_rate_limits`
collection's TTL index was never actually applied (oldest live document:
2026-03-13 — six months of unbounded accumulation), even though a confirmed
reader (`petrosa_otel.ConfigRateLimiter.check_rate_limit`) exists. Mirrors
the `alerts`/`signals` kill-switch pattern so this service's own writer can
be disabled via config + restart with no code change.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from data_manager.main import DataManagerApp


@pytest.fixture
def app_instance():
    app = DataManagerApp()
    app.db_manager = Mock()
    app.db_manager.mongodb_adapter = Mock()
    app.backfill_orchestrator = None
    return app


async def _run_with_rate_limiter_mock(app_instance, monkeypatch, env_value=None):
    """Drive `_run_api_server` far enough to reach the rate-limiter wiring,
    then cancel before it would block on `uvicorn.Server.serve()`."""
    if env_value is None:
        monkeypatch.delenv("CONFIG_RATE_LIMIT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CONFIG_RATE_LIMIT_ENABLED", env_value)

    fake_rate_limiter_cls = MagicMock()
    fake_app = MagicMock()
    fake_app.state = MagicMock()

    fake_server = MagicMock()
    fake_server.serve = AsyncMock(side_effect=__import__("asyncio").CancelledError())

    with (
        patch("data_manager.main.create_app", return_value=fake_app),
        patch("data_manager.api.routes.backfill"),
        patch("data_manager.api.routes.config"),
        patch("data_manager.api"),
        patch("petrosa_otel.ConfigRateLimiter", fake_rate_limiter_cls, create=True),
        patch("data_manager.main.uvicorn.Config", return_value=MagicMock()),
        patch("data_manager.main.uvicorn.Server", return_value=fake_server),
    ):
        await app_instance._run_api_server()

    return fake_rate_limiter_cls


@pytest.mark.asyncio
async def test_rate_limiter_enabled_by_default(app_instance, monkeypatch):
    fake_cls = await _run_with_rate_limiter_mock(app_instance, monkeypatch)
    fake_cls.assert_called_once()
    _args, kwargs = fake_cls.call_args
    assert kwargs["enabled"] is True


@pytest.mark.asyncio
async def test_rate_limiter_disabled_via_env(app_instance, monkeypatch):
    fake_cls = await _run_with_rate_limiter_mock(
        app_instance, monkeypatch, env_value="false"
    )
    fake_cls.assert_called_once()
    _args, kwargs = fake_cls.call_args
    assert kwargs["enabled"] is False


@pytest.mark.asyncio
async def test_rate_limiter_enabled_explicit_true(app_instance, monkeypatch):
    fake_cls = await _run_with_rate_limiter_mock(
        app_instance, monkeypatch, env_value="true"
    )
    _args, kwargs = fake_cls.call_args
    assert kwargs["enabled"] is True
