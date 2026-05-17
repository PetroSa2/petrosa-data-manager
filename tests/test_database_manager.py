"""
Unit tests for data_manager.db.database_manager.DatabaseManager.

Mocks get_adapter; verifies initialization, health_check shape, reconnection
backoff with max-attempts guard, statistics, property guards, and the async
context manager.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.db.database_manager import DatabaseManager


def make_adapter():
    """Build a mock that satisfies both sync (mysql) and async (mongo) shapes."""
    adapter = MagicMock()
    adapter.connect = MagicMock()
    adapter.disconnect = MagicMock()
    adapter.is_connected = MagicMock(return_value=True)
    return adapter


class TestInit:
    def test_initial_state(self):
        dm = DatabaseManager()
        assert dm.mysql_adapter is None
        assert dm.mongodb_adapter is None
        assert dm.configuration is None
        assert dm._initialized is False
        assert dm._connection_start_time is None
        assert dm._stats["mysql"]["connection_count"] == 0


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_sets_both_adapters(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            mysql_a = make_adapter()
            mongo_a = make_adapter()
            get_adp.side_effect = [mysql_a, mongo_a]
            dm = DatabaseManager()
            await dm.initialize()
            assert dm._initialized is True
            assert dm.mysql_adapter is mysql_a
            assert dm.mongodb_adapter is mongo_a
            assert dm.configuration is not None
            assert dm._stats["mysql"]["connection_count"] == 1
            assert dm._stats["mongodb"]["connection_count"] == 1
            # Health monitor task should be scheduled.
            assert dm._health_check_task is not None
            await dm.shutdown()

    @pytest.mark.asyncio
    async def test_initialize_propagates_mysql_error(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            mysql_a = MagicMock()
            mysql_a.connect.side_effect = RuntimeError("mysql down")
            get_adp.return_value = mysql_a
            dm = DatabaseManager()
            with pytest.raises(RuntimeError, match="mysql down") as exc_info:
                await dm.initialize()
            assert "mysql down" in str(exc_info.value)
            # Shutdown was called on failure → not initialized.
            assert dm._initialized is False


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_disconnects_both_adapters(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            mysql_a = make_adapter()
            mongo_a = make_adapter()
            get_adp.side_effect = [mysql_a, mongo_a]
            dm = DatabaseManager()
            await dm.initialize()
            await dm.shutdown()
            mysql_a.disconnect.assert_called_once()
            mongo_a.disconnect.assert_called_once()
            assert dm._initialized is False

    @pytest.mark.asyncio
    async def test_shutdown_swallows_disconnect_errors(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            mysql_a = make_adapter()
            mysql_a.disconnect.side_effect = RuntimeError("disconnect failed")
            mongo_a = make_adapter()
            get_adp.side_effect = [mysql_a, mongo_a]
            dm = DatabaseManager()
            await dm.initialize()
            # Must not raise.
            await dm.shutdown()
            mongo_a.disconnect.assert_called_once()


class TestHealthCheck:
    def test_returns_disconnected_when_no_adapters(self):
        dm = DatabaseManager()
        result = dm.health_check()
        assert result["mysql"]["connected"] is False
        assert result["mongodb"]["connected"] is False
        assert result["initialized"] is False

    def test_returns_connected_when_adapters_healthy(self):
        dm = DatabaseManager()
        dm.mysql_adapter = make_adapter()
        dm.mongodb_adapter = make_adapter()
        result = dm.health_check()
        assert result["mysql"]["connected"] is True
        assert result["mongodb"]["connected"] is True

    def test_is_healthy_requires_both_connected(self):
        dm = DatabaseManager()
        # Both connected
        dm.mysql_adapter = make_adapter()
        dm.mongodb_adapter = make_adapter()
        assert dm.is_healthy() is True
        # MySQL down
        dm.mysql_adapter.is_connected = MagicMock(return_value=False)
        assert dm.is_healthy() is False


class TestSyncContextManager:
    def test_enter_exit_no_op(self):
        dm = DatabaseManager()
        with dm as ctx:
            assert ctx is dm
        # Exit is a no-op — no exception, no state change.
        assert dm._initialized is False


class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_aenter_initializes(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            get_adp.side_effect = [make_adapter(), make_adapter()]
            dm = DatabaseManager()
            async with dm as ctx:
                assert ctx is dm
                assert dm._initialized is True
            # After exit, shutdown ran.
            assert dm._initialized is False


class TestReconnectMysql:
    @pytest.mark.asyncio
    async def test_reconnect_succeeds_resets_attempts(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            with patch("data_manager.db.database_manager.constants") as const:
                const.DB_RECONNECT_MAX_ATTEMPTS = 5
                const.DB_RECONNECT_BACKOFF_BASE = 1.0  # 1.0**n == 1.0 (fast)
                const.MYSQL_URI = "mysql://x"
                # Patch asyncio.sleep to avoid actual waits
                with patch("asyncio.sleep", new=AsyncMock()):
                    new_mysql = make_adapter()
                    get_adp.return_value = new_mysql
                    dm = DatabaseManager()
                    dm._mysql_reconnect_attempts = 1
                    await dm._reconnect_mysql()
                    assert dm.mysql_adapter is new_mysql
                    # Reset counter on success.
                    assert dm._mysql_reconnect_attempts == 0
                    assert dm._stats["mysql"]["reconnect_attempts"] == 1

    @pytest.mark.asyncio
    async def test_reconnect_records_error_on_failure(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            with patch("data_manager.db.database_manager.constants") as const:
                const.DB_RECONNECT_MAX_ATTEMPTS = 5
                const.DB_RECONNECT_BACKOFF_BASE = 1.0
                const.MYSQL_URI = "mysql://x"
                with patch("asyncio.sleep", new=AsyncMock()):
                    failing = make_adapter()
                    failing.connect.side_effect = RuntimeError("still down")
                    get_adp.return_value = failing
                    dm = DatabaseManager()
                    # Must not raise.
                    await dm._reconnect_mysql()
                    assert dm._stats["mysql"]["error_count"] == 1

    @pytest.mark.asyncio
    async def test_reconnect_bails_after_max_attempts(self):
        with patch("data_manager.db.database_manager.constants") as const:
            const.DB_RECONNECT_MAX_ATTEMPTS = 3
            dm = DatabaseManager()
            dm._mysql_reconnect_attempts = 3
            # No adapter mutation should occur.
            await dm._reconnect_mysql()
            assert dm.mysql_adapter is None


class TestReconnectMongodb:
    @pytest.mark.asyncio
    async def test_reconnect_succeeds(self):
        with patch("data_manager.db.database_manager.get_adapter") as get_adp:
            with patch("data_manager.db.database_manager.constants") as const:
                const.DB_RECONNECT_MAX_ATTEMPTS = 5
                const.DB_RECONNECT_BACKOFF_BASE = 1.0
                const.MONGODB_URL = "mongodb://x"
                with patch("asyncio.sleep", new=AsyncMock()):
                    new_mongo = make_adapter()
                    get_adp.return_value = new_mongo
                    dm = DatabaseManager()
                    await dm._reconnect_mongodb()
                    assert dm.mongodb_adapter is new_mongo
                    assert dm._mongodb_reconnect_attempts == 0

    @pytest.mark.asyncio
    async def test_reconnect_bails_after_max_attempts(self):
        with patch("data_manager.db.database_manager.constants") as const:
            const.DB_RECONNECT_MAX_ATTEMPTS = 2
            dm = DatabaseManager()
            dm._mongodb_reconnect_attempts = 2
            await dm._reconnect_mongodb()
            assert dm.mongodb_adapter is None


class TestConnectionStats:
    def test_includes_uptime_when_started(self):
        dm = DatabaseManager()
        dm._connection_start_time = 1000.0
        with patch("time.time", return_value=1100.0):
            stats = dm.get_connection_stats()
            assert stats["overall"]["uptime_seconds"] == pytest.approx(100.0)
            assert stats["overall"]["initialized"] is False
            assert "databases" in stats

    def test_uptime_zero_before_init(self):
        dm = DatabaseManager()
        stats = dm.get_connection_stats()
        assert stats["overall"]["uptime_seconds"] == 0


class TestIncrementCounters:
    def test_increment_query_count(self):
        dm = DatabaseManager()
        dm.increment_query_count("mysql")
        dm.increment_query_count("mysql")
        dm.increment_query_count("mongodb")
        assert dm._stats["mysql"]["query_count"] == 2
        assert dm._stats["mongodb"]["query_count"] == 1

    def test_increment_unknown_database_is_noop(self):
        dm = DatabaseManager()
        dm.increment_query_count("redis")  # Not a tracked DB.
        # State unchanged.
        assert "redis" not in dm._stats

    def test_increment_error_count(self):
        dm = DatabaseManager()
        dm.increment_error_count("mysql")
        assert dm._stats["mysql"]["error_count"] == 1


class TestProperties:
    def test_mongodb_property_raises_when_not_initialized(self):
        dm = DatabaseManager()
        with pytest.raises(
            RuntimeError, match="MongoDB adapter not initialized"
        ) as exc_info:
            _ = dm.mongodb
        assert "MongoDB" in str(exc_info.value)

    def test_mongodb_property_returns_adapter(self):
        dm = DatabaseManager()
        dm.mongodb_adapter = make_adapter()
        assert dm.mongodb is dm.mongodb_adapter

    def test_mysql_property_raises_when_not_initialized(self):
        dm = DatabaseManager()
        with pytest.raises(
            RuntimeError, match="MySQL adapter not initialized"
        ) as exc_info:
            _ = dm.mysql
        assert "MySQL" in str(exc_info.value)

    def test_mysql_property_returns_adapter(self):
        dm = DatabaseManager()
        dm.mysql_adapter = make_adapter()
        assert dm.mysql is dm.mysql_adapter
