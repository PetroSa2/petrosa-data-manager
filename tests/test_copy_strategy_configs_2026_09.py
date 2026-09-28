from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.maintenance.copy_strategy_configs_2026_09 import (
    _main,
    _parser,
    copy_strategy_configs,
    preferred_document,
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


@pytest.mark.asyncio
async def test_legacy_symbol_document_targets_symbol_collection_and_audits_deduplicate():
    client = MagicMock()
    source = _Db()
    target = _Db()
    source["strategy_configs_global"] = _collection([])
    source["strategy_configs_symbol"] = _collection([])
    source["strategy_config_audit"] = _collection([{"_id": "audit-1"}])
    legacy = _collection(
        [{"strategy_id": "s1", "symbol": "BTCUSDT", "side": "LONG", "version": 1}]
    )
    target["strategy_configs"] = legacy
    target["strategy_configs_global"] = _collection([])
    target["strategy_configs_symbol"] = _collection([])
    target["strategy_config_audit"] = _collection([])
    target["strategy_config_audit"].find_one = AsyncMock(
        return_value={"_id": "audit-1"}
    )
    client.__getitem__.side_effect = [source, target]

    results = await copy_strategy_configs(client, dry_run=False)

    assert results["strategy_configs_legacy"]["copied"] == 1
    target["strategy_configs_symbol"].replace_one.assert_called_once()
    target["strategy_config_audit"].insert_one.assert_not_called()


def test_preferred_document_skips_older_candidate():
    existing = {"version": 2}
    candidate = {"version": 1}

    winner, should_copy, conflict = preferred_document(existing, candidate)

    assert winner is existing
    assert should_copy is False
    assert conflict is True


@pytest.mark.asyncio
async def test_copy_reports_older_conflict_and_copies_new_audit():
    client = MagicMock()
    source = _Db()
    target = _Db()
    source_global = _collection([{"strategy_id": "s1", "version": 1}])
    source["strategy_configs_global"] = source_global
    source["strategy_configs_symbol"] = _collection([])
    source["strategy_config_audit"] = _collection([{"_id": "audit-2"}])
    target_global = _collection([])
    target_global.find_one = AsyncMock(return_value={"strategy_id": "s1", "version": 2})
    target["strategy_configs"] = _collection([])
    target["strategy_configs_global"] = target_global
    target["strategy_configs_symbol"] = _collection([])
    target["strategy_config_audit"] = _collection([])
    client.__getitem__.side_effect = [source, target]

    results = await copy_strategy_configs(client, dry_run=False)

    assert results["strategy_configs_global"]["skipped_older"] == 1
    target["strategy_config_audit"].insert_one.assert_awaited_once_with(
        {"_id": "audit-2"}
    )


def test_parser_defaults_to_dry_run():
    args = _parser().parse_args([])

    assert args.apply is False
    assert args.source_db == "petrosa"


@pytest.mark.asyncio
async def test_main_closes_client(monkeypatch, capsys):
    client = MagicMock()

    class ClientFactory:
        def __new__(cls, _url):
            return client

    async def fake_copy(*_args, **_kwargs):
        return {"global": {"copied": 1, "skipped_older": 0, "conflicts": 0}}

    import data_manager.maintenance.copy_strategy_configs_2026_09 as module

    monkeypatch.setattr(module, "AsyncIOMotorClient", ClientFactory)
    monkeypatch.setattr(module, "copy_strategy_configs", fake_copy)
    await _main(_parser().parse_args(["--apply"]))

    client.close.assert_called_once()
    assert "global: copied=1" in capsys.readouterr().out
