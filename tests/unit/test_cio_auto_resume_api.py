from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import data_manager.maintenance.import_cio_auto_resume as importer
from data_manager.api.routes import cio_state
from data_manager.api.routes.cio_state import CioPauseEntry
from data_manager.db.repositories.cio_auto_resume_repository import (
    CioAutoResumeRepository,
)
from data_manager.maintenance.import_cio_auto_resume import import_entries, read_entries


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return [dict(row) for row in self.rows]


class Collection:
    def __init__(self):
        self.rows = {}

    def find(self, query):
        rows = list(self.rows.values())
        if "$in" in query.get("status", {}):
            rows = [row for row in rows if row["status"] in query["status"]["$in"]]
        return Cursor(rows)

    async def find_one(self, query):
        row = self.rows.get(query["strategy_id"])
        return dict(row) if row else None

    async def replace_one(self, query, document, upsert=False):
        self.rows[query["strategy_id"]] = dict(document)

    async def delete_one(self, query):
        removed = self.rows.pop(query["strategy_id"], None)
        return SimpleNamespace(deleted_count=bool(removed))


def entry(strategy_id="s-1", status="paused"):
    return CioPauseEntry(
        strategy_id=strategy_id,
        service="cio",
        status=status,
        paused_at=1.25,
        last_unavailable_at=2.5,
        min_pause_seconds=30,
        flap_count=2,
        attempts=1,
        next_attempt_at=3.75,
    )


@pytest.fixture
def repository(monkeypatch):
    collection = Collection()
    repo = CioAutoResumeRepository(
        None, SimpleNamespace(db={"cio_auto_resume_registry": collection})
    )
    monkeypatch.setattr(cio_state, "_repo", lambda: repo)
    return repo


@pytest.mark.asyncio
async def test_put_get_filter_and_delete_round_trip(repository):
    await cio_state.put_entry(entry(), "s-1")
    await cio_state.put_entry(entry("s-2", "retrying"), "s-2")
    listed = await cio_state.list_entries("paused,retrying")
    assert listed["count"] == 2
    assert listed["entries"][0]["paused_at"] == 1.25
    assert (await cio_state.get_entry("s-1"))["strategy_id"] == "s-1"
    assert await cio_state.delete_entry("s-1", "recovered") == {"removed": True}
    assert await cio_state.delete_entry("s-1", "missing") == {"removed": False}


@pytest.mark.asyncio
async def test_validation_errors(repository):
    with pytest.raises(HTTPException) as mismatch:
        await cio_state.put_entry(entry("s-1"), "s-2")
    assert mismatch.value.status_code == 422
    with pytest.raises(HTTPException) as missing:
        await cio_state.get_entry("missing")
    assert missing.value.status_code == 404
    with pytest.raises(ValueError):
        CioPauseEntry.model_validate({**entry().model_dump(), "extra": True})


@pytest.mark.asyncio
async def test_database_errors_are_503(monkeypatch):
    class Broken:
        async def list_entries(self, _status):
            raise RuntimeError("down")

        async def get_entry(self, _strategy_id):
            raise RuntimeError("down")

        async def upsert_entry(self, _entry):
            raise RuntimeError("down")

        async def delete_entry(self, _strategy_id):
            raise RuntimeError("down")

    monkeypatch.setattr(cio_state, "_repo", lambda: Broken())
    for call in (
        cio_state.list_entries(),
        cio_state.get_entry("s-1"),
        cio_state.put_entry(entry(), "s-1"),
        cio_state.delete_entry("s-1"),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 503


def test_import_reader_and_dry_run(tmp_path: Path):
    source = tmp_path / "entries.json"
    source.write_text(json.dumps({"s-1": json.dumps(entry().model_dump(mode="json"))}))
    entries = read_entries(source)
    assert len(entries) == 1
    assert __import__("asyncio").run(import_entries(entries, apply=False)) == 1


def test_import_apply_upserts(monkeypatch):
    calls = []

    class Adapter:
        def __init__(self, connection_string):
            self.connection_string = connection_string

        def connect(self):
            pass

        def disconnect(self):
            pass

    async def upsert(self, value):
        calls.append(value)

    monkeypatch.setattr(importer, "MongoDBAdapter", Adapter)
    monkeypatch.setattr(CioAutoResumeRepository, "upsert_entry", upsert)
    count = __import__("asyncio").run(
        import_entries([entry().model_dump()], apply=True)
    )
    assert count == 1
    assert len(calls) == 1
