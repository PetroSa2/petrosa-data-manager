"""
Tests for ConfigurationRepository in Data Manager.
"""

from datetime import datetime, timezone
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

    # Mock collections
    mongodb.db.app_config = MagicMock()
    mongodb.db.app_config_audit = MagicMock()
    mongodb.db.strategy_configs = MagicMock()
    mongodb.db.strategy_config_audit = MagicMock()

    return mongodb


@pytest.mark.asyncio
async def test_upsert_app_config(mock_mongodb):
    # Setup
    mock_mongodb.db.app_config.find_one = AsyncMock(return_value=None)
    mock_mongodb.db.app_config.replace_one = AsyncMock()
    mock_mongodb.db.app_config_audit.insert_one = AsyncMock()

    repo = ConfigurationRepository(mongodb_adapter=mock_mongodb, mysql_adapter=None)
    params = {"enabled": True}

    # Execute
    result = await repo.upsert_app_config(params, "test_user", "Test reason")

    # Verify
    assert result["parameters"] == params
    assert result["version"] == 1
    mock_mongodb.db.app_config.replace_one.assert_called_once()
    mock_mongodb.db.app_config_audit.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_rollback_app_config(mock_mongodb):
    # Setup
    # Current version is 2, rollback to version 1
    v1_params = {"val": 1}
    v2_params = {"val": 2}

    v1_record = {"new_parameters": v1_params, "version": 1}
    v2_record = {"new_parameters": v2_params, "version": 2}

    # Mock find().sort().skip().limit().to_list() for finding previous version
    # Motor cursor: find()/sort()/skip()/limit() are sync, to_list() is async
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[v1_record])

    audit_collection = MagicMock()
    audit_collection.find.return_value = cursor
    audit_collection.insert_one = AsyncMock()

    # The rollback method accesses collection via db[name] (dict-style)
    mock_mongodb.db.__getitem__.return_value = audit_collection
    mock_mongodb.db.app_config_audit = audit_collection

    # find_one inside get_app_config (called from upsert_app_config during rollback)
    mock_mongodb.db.app_config.find_one = AsyncMock(return_value=None)
    mock_mongodb.db.app_config.replace_one = AsyncMock()

    repo = ConfigurationRepository(mongodb_adapter=mock_mongodb, mysql_adapter=None)

    # Execute
    success, error, config = await repo.rollback("application", "admin")

    # Verify
    assert success is True
    assert config["parameters"] == v1_params
