from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from data_manager.api.routes import service_config
from data_manager.api.routes.service_config import ServiceConfigRequest
from data_manager.db.repositories.service_config_repository import (
    ServiceConfigRepository,
)


class FakeCursor:
    def __init__(self, values):
        self.values = values

    def sort(self, *_args):
        return self

    def limit(self, _limit):
        return self

    def __aiter__(self):
        return self._items()

    async def _items(self):
        for value in self.values:
            yield value


class FakeCollection:
    def __init__(self):
        self.documents = {}
        self.audit = []

    def find(self, query):
        return FakeCursor(
            [
                doc
                for doc in self.documents.values()
                if all(doc[k] == v for k, v in query.items())
            ]
        )

    async def find_one(self, query):
        return next(
            (
                doc
                for doc in self.documents.values()
                if all(doc[k] == v for k, v in query.items())
            ),
            None,
        )

    async def find_one_and_update(self, query, update, **_kwargs):
        current = await self.find_one(query)
        if current is None and _kwargs.get("upsert"):
            current = {"service": query["service"], "key": query["key"], "version": 0}
            self.documents[(current["service"], current["key"])] = current
        if current is None:
            return None
        current.update(update["$set"])
        current["version"] += update["$inc"]["version"]
        return current

    async def insert_one(self, document):
        self.documents[(document["service"], document["key"])] = document
        self.audit.append(document)

    async def create_index(self, *_args, **_kwargs):
        return "index"


@pytest.fixture
def repository():
    db = {"service_configs": FakeCollection(), "service_config_audit": FakeCollection()}
    repo = ServiceConfigRepository(SimpleNamespace(db=db))
    repo.audit.insert_one = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_put_and_audit_are_written(repository):
    result = await repository.put(
        "binance-data-extractor", "symbols", ["BTCUSDT"], "ops", "seed", None
    )
    assert result["version"] == 1
    repository.audit.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_list_get_and_audit(repository):
    await repository.put("service", "key", {"enabled": True}, "ops", None, None)
    assert (await repository.get("service", "key"))["value"] == {"enabled": True}
    assert len(await repository.list("service")) == 1
    repository.audit.documents[("service", "key")] = {
        "service": "service",
        "key": "key",
        "changed_at": "now",
    }
    assert len(await repository.audit_entries("service", None, 100)) == 1


@pytest.mark.asyncio
async def test_routes_validate_and_return_values(monkeypatch):
    fake = SimpleNamespace(
        list=AsyncMock(
            return_value=[
                {"service": "svc", "key": "enabled", "value": True, "version": 1}
            ]
        ),
        get=AsyncMock(return_value=None),
        put=AsyncMock(
            return_value={
                "service": "svc",
                "key": "enabled",
                "value": True,
                "version": 1,
            }
        ),
        audit_entries=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(service_config, "_repository", lambda: fake)
    assert (await service_config.get_service_config("svc"))["keys"]["enabled"][
        "value"
    ] is True
    with pytest.raises(HTTPException) as missing:
        await service_config.get_service_config_key("svc", "enabled")
    assert missing.value.status_code == 404
    response = await service_config.put_service_config(
        "svc", "enabled", ServiceConfigRequest(value=True, changed_by="ops")
    )
    assert response["version"] == 1
    assert (await service_config.get_service_config_audit("svc")) == {"entries": []}


@pytest.mark.asyncio
async def test_routes_reject_bad_service_and_large_values(monkeypatch):
    monkeypatch.setattr(service_config, "_repository", lambda: None)
    with pytest.raises(HTTPException) as bad:
        await service_config.get_service_config("Bad_Name")
    assert bad.value.status_code == 422
    with pytest.raises(HTTPException) as large:
        await service_config.put_service_config(
            "svc",
            "key",
            ServiceConfigRequest(value="x" * (64 * 1024), changed_by="ops"),
        )
    assert large.value.status_code == 413
