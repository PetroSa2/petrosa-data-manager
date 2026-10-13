from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.main import DataManagerApp


@pytest.mark.asyncio
async def test_start_wires_completeness_scheduler_and_stop_cleans_it_up():
    app = DataManagerApp()
    app._shutdown_event.set()
    app._start_loop_lag_monitor = MagicMock()
    app._check_schema_migrations = AsyncMock()
    app._ensure_mongodb_indexes = AsyncMock()
    app._start_mysql_server_status_collector = MagicMock()
    app._wire_route_publishers = MagicMock()
    app._run_auditor = AsyncMock()
    app._run_analytics = AsyncMock()
    app._run_drawdown_scheduler = AsyncMock()
    app._run_candle_warmup_scheduler = AsyncMock()
    app._run_ledger_tieout_scheduler = AsyncMock()
    app._run_mongo_data_size_loop = AsyncMock()

    database_manager = MagicMock()
    database_manager.initialize = AsyncMock()
    database_manager.mongodb_adapter = None
    database_manager.mysql_adapter = None
    market_consumer = MagicMock()
    market_consumer.start = AsyncMock(return_value=False)
    market_consumer.stop = AsyncMock()

    async def completeness_loop(*args, **kwargs):
        await app._shutdown_event.wait()

    async def freshness_loop(*args, **kwargs):
        await app._shutdown_event.wait()

    async def daily_completeness_loop(*args, **kwargs):
        await app._shutdown_event.wait()

    with (
        patch("data_manager.main.DatabaseManager", return_value=database_manager),
        patch("data_manager.main.MarketDataConsumer", return_value=market_consumer),
        patch("data_manager.main.start_http_server"),
        patch("data_manager.main.constants.ENABLE_API", False),
        patch("data_manager.main.constants.ENABLE_REPORT_PRECOMPUTE", False),
        patch("data_manager.main.constants.ENABLE_BACKFILLER", False),
        patch("data_manager.main.constants.ENABLE_INTENT_CONSUMER", False),
        patch("data_manager.main.constants.ENABLE_DECISION_CONSUMER", False),
        patch("data_manager.main.constants.ENABLE_ALERT_DISPATCHER", False),
        patch("data_manager.main.constants.ENABLE_EXECUTION_EVENTS_CONSUMER", False),
        patch("data_manager.main.constants.ENABLE_EXECUTION_EVALUATOR", False),
        patch("data_manager.main.constants.ENABLE_AUDIT_EVALUATOR", False),
        patch("data_manager.main.constants.ENABLE_PNL_CONSUMER", False),
        patch("data_manager.main.constants.ENABLE_MONGO_DATA_SIZE_GAUGE", False),
        patch(
            "data_manager.maintenance.klines_mysql_freshness.freshness_loop",
            freshness_loop,
        ),
        patch(
            "data_manager.maintenance.klines_completeness.completeness_loop",
            completeness_loop,
        ),
        patch(
            "data_manager.maintenance.klines_daily_gaps.daily_completeness_loop",
            daily_completeness_loop,
        ),
        patch("data_manager.auditor.gap_detector.GapDetector"),
    ):
        await app.start()

    assert app.klines_completeness_task is not None

    app.db_manager.shutdown = AsyncMock()
    await app.stop()

    assert app.klines_completeness_task is None
    database_manager.shutdown.assert_awaited_once_with()
