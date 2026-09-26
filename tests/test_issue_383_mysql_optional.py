from unittest.mock import Mock, patch

import pytest

from data_manager.main import DataManagerApp


def app_with_unhealthy_mongo() -> DataManagerApp:
    app = DataManagerApp.__new__(DataManagerApp)
    app.db_manager = Mock(
        mongodb_adapter=Mock(),
        mongo_healthy=Mock(return_value=False),
    )
    app.consumer = None
    return app


@pytest.mark.asyncio
async def test_auditor_uses_mongo_health():
    app = app_with_unhealthy_mongo()
    with patch("data_manager.main.constants.ENABLE_AUDITOR", True):
        await app._run_auditor()
    assert app.db_manager.mongo_healthy.called


@pytest.mark.asyncio
async def test_analytics_uses_mongo_health():
    app = app_with_unhealthy_mongo()
    with patch("data_manager.main.constants.ENABLE_ANALYTICS", True):
        await app._run_analytics()
    assert app.db_manager.mongo_healthy.called


@pytest.mark.asyncio
async def test_drawdown_uses_mongo_health():
    app = app_with_unhealthy_mongo()
    await app._run_drawdown_scheduler()
    assert app.db_manager.mongo_healthy.called


@pytest.mark.asyncio
async def test_candle_warmup_uses_mongo_health():
    app = app_with_unhealthy_mongo()
    app.leader_election = Mock()
    app.candle_warmup_scheduler = None
    with patch("data_manager.main.constants.ENABLE_CANDLE_WARMUP_SCHEDULER", True):
        await app._run_candle_warmup_scheduler()
    assert app.db_manager.mongo_healthy.called
