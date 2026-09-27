from types import SimpleNamespace

import pytest

from data_manager.maintenance.copy_extractor_config_2026_09 import copy_extractor_config


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        return self.items()

    async def items(self):
        for doc in self.docs:
            yield doc


class Collection:
    def __init__(self, docs=()):
        self.docs = list(docs)
        self.writes = []

    def find(self, _query):
        return Cursor(self.docs)

    async def find_one(self, query):
        return next(
            (doc for doc in self.writes if all(doc[k] == v for k, v in query.items())),
            None,
        )

    async def insert_one(self, document):
        self.writes.append(document)


@pytest.mark.asyncio
async def test_copy_dry_run_and_apply_skip_existing(monkeypatch):
    source = Collection(
        [
            {"key": "symbols", "value": ["BTCUSDT"], "changed_by": "extractor"},
            {"name": "rate_limits", "value": {"requests": 10}},
        ]
    )
    target = Collection()
    client = {
        "petrosa_config": {"data_extractor_config": source},
        "petrosa_data": {"service_configs": target},
    }
    monkeypatch.setattr("constants.CANDLE_MONGO_DATABASE", "petrosa_data")
    result = await copy_extractor_config(client)
    assert result == {"copied": 2, "skipped": 0}
    assert target.writes == []
    result = await copy_extractor_config(client, dry_run=False)
    assert result["copied"] == 2
    result = await copy_extractor_config(client, dry_run=False)
    assert result["skipped"] == 2
