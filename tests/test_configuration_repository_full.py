"""
Comprehensive coverage for data_manager.db.repositories.configuration_repository.

Existing test_configuration_repository.py covers upsert_app_config and rollback
narrowly. This file fills in get_app_config, get_strategy_config,
upsert_strategy_config (CREATE/UPDATE/ROLLBACK), get_audit_trail with filters,
disconnected-mongo guards, and exception handlers.
"""

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.db.repositories.configuration_repository import (
    ConfigurationRepository,
)


@pytest.fixture
def mock_mongodb():
    mongodb = MagicMock()
    mongodb.is_connected = True
    mongodb.db = MagicMock()
    # Mock collections used by the repository.
    for name in (
        "app_config",
        "app_config_audit",
        "strategy_configs",
        "strategy_config_audit",
    ):
        coll = MagicMock()
        setattr(mongodb.db, name, coll)
    # Also support db[name] dict-style access for get_audit_trail / rollback.
    mongodb.db.__getitem__.side_effect = lambda name: getattr(mongodb.db, name)
    return mongodb


class TestGetAppConfig:
    @pytest.mark.asyncio
    async def test_returns_none_when_mongodb_is_none(self):
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=None)
        assert await repo.get_app_config() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disconnected(self, mock_mongodb):
        mock_mongodb.is_connected = False
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.get_app_config() is None

    @pytest.mark.asyncio
    async def test_returns_config_and_strips_id(self, mock_mongodb):
        mock_mongodb.db.app_config.find_one = AsyncMock(
            return_value={"_id": "abc", "parameters": {"k": "v"}, "version": 3}
        )
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        result = await repo.get_app_config()
        assert result["parameters"] == {"k": "v"}
        assert "_id" not in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_record(self, mock_mongodb):
        mock_mongodb.db.app_config.find_one = AsyncMock(return_value=None)
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.get_app_config() is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, mock_mongodb):
        mock_mongodb.db.app_config.find_one = AsyncMock(side_effect=RuntimeError("x"))
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.get_app_config() is None


class TestUpsertAppConfig:
    @pytest.mark.asyncio
    async def test_returns_none_when_disconnected(self):
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=None)
        assert await repo.upsert_app_config({}, "user") is None

    @pytest.mark.asyncio
    async def test_create_action_when_no_existing(self, mock_mongodb):
        mock_mongodb.db.app_config.find_one = AsyncMock(return_value=None)
        mock_mongodb.db.app_config.replace_one = AsyncMock()
        mock_mongodb.db.app_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        result = await repo.upsert_app_config({"key": "v"}, "user", reason="initial")
        assert result is not None
        assert result["version"] == 1
        assert result["parameters"] == {"key": "v"}
        mock_mongodb.db.app_config_audit.insert_one.assert_called_once()
        audit_arg = mock_mongodb.db.app_config_audit.insert_one.call_args[0][0]
        assert audit_arg["action"] == "CREATE"
        assert audit_arg["version"] == 1

    @pytest.mark.asyncio
    async def test_update_action_increments_version(self, mock_mongodb):
        mock_mongodb.db.app_config.find_one = AsyncMock(
            return_value={
                "parameters": {"old": True},
                "version": 4,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )
        mock_mongodb.db.app_config.replace_one = AsyncMock()
        mock_mongodb.db.app_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        result = await repo.upsert_app_config({"new": True}, "user")
        assert result["version"] == 5
        audit_arg = mock_mongodb.db.app_config_audit.insert_one.call_args[0][0]
        assert audit_arg["action"] == "UPDATE"

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, mock_mongodb):
        mock_mongodb.db.app_config.find_one = AsyncMock(side_effect=RuntimeError("x"))
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.upsert_app_config({"k": "v"}, "user") is None


class TestGetStrategyConfig:
    @pytest.mark.asyncio
    async def test_returns_none_when_disconnected(self):
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=None)
        assert await repo.get_strategy_config("s1") is None

    @pytest.mark.asyncio
    async def test_returns_config_and_strips_id(self, mock_mongodb):
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(
            return_value={"_id": "x", "strategy_id": "s1", "parameters": {"a": 1}}
        )
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        result = await repo.get_strategy_config("s1", symbol="BTCUSDT", side="long")
        assert result["strategy_id"] == "s1"
        assert "_id" not in result
        # Query passes all three filters.
        called_with = mock_mongodb.db.strategy_configs.find_one.call_args[0][0]
        assert called_with["strategy_id"] == "s1"
        assert called_with["symbol"] == "BTCUSDT"
        assert called_with["side"] == "long"

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, mock_mongodb):
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.get_strategy_config("s1") is None


class TestUpsertStrategyConfig:
    @pytest.mark.asyncio
    async def test_returns_none_when_disconnected(self):
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=None)
        assert await repo.upsert_strategy_config("s1", {}, "user") is None

    @pytest.mark.asyncio
    async def test_create_when_no_existing(self, mock_mongodb):
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(return_value=None)
        mock_mongodb.db.strategy_configs.replace_one = AsyncMock()
        mock_mongodb.db.strategy_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        result = await repo.upsert_strategy_config("s1", {"a": 1}, "user")
        assert result is not None
        assert result["version"] == 1
        audit_arg = mock_mongodb.db.strategy_config_audit.insert_one.call_args[0][0]
        assert audit_arg["action"] == "CREATE"

    @pytest.mark.asyncio
    async def test_update_increments_version(self, mock_mongodb):
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(
            return_value={"strategy_id": "s1", "parameters": {}, "version": 2}
        )
        mock_mongodb.db.strategy_configs.replace_one = AsyncMock()
        mock_mongodb.db.strategy_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        result = await repo.upsert_strategy_config("s1", {"a": 2}, "user")
        assert result["version"] == 3
        audit_arg = mock_mongodb.db.strategy_config_audit.insert_one.call_args[0][0]
        assert audit_arg["action"] == "UPDATE"

    @pytest.mark.asyncio
    async def test_rollback_action_label(self, mock_mongodb):
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(
            return_value={"strategy_id": "s1", "parameters": {}, "version": 1}
        )
        mock_mongodb.db.strategy_configs.replace_one = AsyncMock()
        mock_mongodb.db.strategy_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        await repo.upsert_strategy_config("s1", {"a": 3}, "user", action="ROLLBACK")
        audit_arg = mock_mongodb.db.strategy_config_audit.insert_one.call_args[0][0]
        assert audit_arg["action"] == "ROLLBACK"

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, mock_mongodb):
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(
            side_effect=RuntimeError("x")
        )
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.upsert_strategy_config("s1", {"a": 1}, "user") is None


class TestGetAuditTrail:
    @pytest.mark.asyncio
    async def test_returns_empty_when_disconnected(self):
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=None)
        assert await repo.get_audit_trail("application") == []

    @pytest.mark.asyncio
    async def test_returns_records_for_application(self, mock_mongodb):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(
            return_value=[
                {
                    "_id": "abc",
                    "config_type": "application",
                    "changed_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ]
        )
        mock_mongodb.db.app_config_audit.find = MagicMock(return_value=cursor)

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        records = await repo.get_audit_trail("application")
        assert len(records) == 1
        assert records[0]["_id"] == "abc"  # _id was converted to str
        # changed_at must be ISO-formatted
        assert isinstance(records[0]["changed_at"], str)

    @pytest.mark.asyncio
    async def test_returns_records_for_strategy_with_filters(self, mock_mongodb):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[])
        mock_mongodb.db.strategy_config_audit.find = MagicMock(return_value=cursor)

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        await repo.get_audit_trail(
            "strategy", strategy_id="s1", symbol="BTCUSDT", side="long", limit=50
        )
        # Verify the right collection was accessed and the query contained filters.
        query = mock_mongodb.db.strategy_config_audit.find.call_args[0][0]
        assert query["strategy_id"] == "s1"
        assert query["symbol"] == "BTCUSDT"
        assert query["side"] == "long"

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, mock_mongodb):
        mock_mongodb.db.app_config_audit.find = MagicMock(side_effect=RuntimeError("x"))
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        assert await repo.get_audit_trail("application") == []


class TestRollback:
    @pytest.mark.asyncio
    async def test_returns_failure_when_disconnected(self):
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=None)
        ok, msg, result = await repo.rollback("application", "user")
        assert ok is False
        assert "not connected" in (msg or "").lower()
        assert result is None

    @pytest.mark.asyncio
    async def test_rollback_to_specific_version(self, mock_mongodb):
        target_record = {
            "version": 3,
            "new_parameters": {"k": "old"},
        }
        mock_mongodb.db.app_config_audit.find_one = AsyncMock(
            return_value=target_record
        )
        # Subsequent upsert path
        mock_mongodb.db.app_config.find_one = AsyncMock(
            return_value={"parameters": {"k": "current"}, "version": 5}
        )
        mock_mongodb.db.app_config.replace_one = AsyncMock()
        mock_mongodb.db.app_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        ok, msg, result = await repo.rollback(
            "application", "user", target_version=3, reason="reverting bad config"
        )
        assert ok is True
        assert msg is None
        assert result is not None
        # Audit record on rollback should reference the rollback reason.
        audit_arg = mock_mongodb.db.app_config_audit.insert_one.call_args[0][0]
        assert "bad config" in (audit_arg.get("reason") or "")

    @pytest.mark.asyncio
    async def test_rollback_to_previous_version(self, mock_mongodb):
        # No target_version -> use previous (skip current).
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(
            return_value=[{"version": 2, "new_parameters": {"k": "old"}}]
        )
        mock_mongodb.db.app_config_audit.find = MagicMock(return_value=cursor)
        mock_mongodb.db.app_config.find_one = AsyncMock(
            return_value={"parameters": {"k": "current"}, "version": 3}
        )
        mock_mongodb.db.app_config.replace_one = AsyncMock()
        mock_mongodb.db.app_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        ok, msg, result = await repo.rollback("application", "user")
        assert ok is True
        assert msg is None

    @pytest.mark.asyncio
    async def test_rollback_returns_false_when_no_previous(self, mock_mongodb):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[])
        mock_mongodb.db.app_config_audit.find = MagicMock(return_value=cursor)

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        ok, msg, result = await repo.rollback("application", "user")
        assert ok is False
        assert msg is not None
        assert result is None

    @pytest.mark.asyncio
    async def test_rollback_returns_false_when_target_lacks_params(self, mock_mongodb):
        mock_mongodb.db.app_config_audit.find_one = AsyncMock(
            return_value={"version": 1, "new_parameters": None}
        )
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        ok, msg, result = await repo.rollback("application", "user", target_version=1)
        assert ok is False
        assert msg is not None

    @pytest.mark.asyncio
    async def test_rollback_strategy_path(self, mock_mongodb):
        # Strategy rollback uses strategy_config_audit collection.
        mock_mongodb.db.strategy_config_audit.find_one = AsyncMock(
            return_value={"version": 2, "new_parameters": {"k": "v"}}
        )
        # And upsert_strategy_config queries strategy_configs / inserts into strategy_config_audit.
        mock_mongodb.db.strategy_configs.find_one = AsyncMock(return_value=None)
        mock_mongodb.db.strategy_configs.replace_one = AsyncMock()
        mock_mongodb.db.strategy_config_audit.insert_one = AsyncMock()

        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        ok, msg, result = await repo.rollback(
            "strategy", "user", strategy_id="s1", target_version=2
        )
        assert ok is True
        assert msg is None
        assert result is not None

    @pytest.mark.asyncio
    async def test_rollback_returns_false_on_exception(self, mock_mongodb):
        mock_mongodb.db.app_config_audit.find_one = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        repo = ConfigurationRepository(mysql_adapter=None, mongodb_adapter=mock_mongodb)
        ok, msg, result = await repo.rollback("application", "user", target_version=1)
        assert ok is False
        assert msg is not None
        assert "boom" in msg
