from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance.copy_strategy_configs_2026_09 import (
    copy_strategy_configs,
)


def _collection(documents):
    collection = MagicMock()
    collection.find.return_value = _Cursor(documents)
    collection.find_one = AsyncMock(return_value=None)
    collection.replace_one = AsyncMock()
    collection.insert_one = AsyncMock()
    return collection


class _Db(dict):
    def __getattr__(self, name):
        return self[name]


class _Cursor:
    def __init__(self, documents):
        self.documents = documents

    def __aiter__(self):
        return self._iterator()

    async def _iterator(self):
        for document in self.documents:
            yield document


@pytest.mark.asyncio
async def test_dry_run_performs_no_writes():
    client = MagicMock()
    source = _Db()
    target = _Db()
    source["strategy_configs_global"] = _collection([])
    source["strategy_configs_symbol"] = _collection([])
    source["strategy_config_audit"] = _collection([])
    target["strategy_configs"] = _collection([])
    target["strategy_configs_global"] = _collection([])
    target["strategy_configs_symbol"] = _collection([])
    target["strategy_config_audit"] = _collection([])
    client.__getitem__.side_effect = [source, target]

    await copy_strategy_configs(client, dry_run=True)

    for collection in (
        target["strategy_configs_global"],
        target["strategy_configs_symbol"],
        target["strategy_config_audit"],
    ):
        collection.replace_one.assert_not_called()
        collection.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_apply_keeps_higher_version():
    client = MagicMock()
    source = _Db()
    target = _Db()
    candidate = {
        "strategy_id": "s1",
        "parameters": {"a": 2},
        "version": 2,
        "updated_at": datetime.now(UTC),
    }
    source["strategy_configs_global"] = _collection([candidate])
    source["strategy_configs_symbol"] = _collection([])
    source["strategy_config_audit"] = _collection([])
    target_global = _collection([])
    target_global.find_one = AsyncMock(
        return_value={"strategy_id": "s1", "version": 1, "updated_at": datetime.min}
    )
    target["strategy_configs"] = _collection([])
    target["strategy_configs_global"] = target_global
    target["strategy_configs_symbol"] = _collection([])
    target["strategy_config_audit"] = _collection([])
    client.__getitem__.side_effect = [source, target]

    await copy_strategy_configs(client, dry_run=False)

    target_global.replace_one.assert_called()
