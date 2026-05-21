"""Tests for the P3.3 strategy lifecycle timeline (#600).

Covers the repository's merge + cursor logic with an in-memory MongoDB
double and the API endpoint via FastAPI TestClient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.db.repositories.strategy_timeline_repository import (
    StrategyTimelineRepository,
)

T0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


class _Collection:
    """In-memory MongoDB collection double with the methods we touch."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def find(self, query: dict[str, Any]) -> _Cursor:
        results = []
        for doc in self._docs:
            ok = True
            for key, val in query.items():
                if key == "strategy_id":
                    if doc.get("strategy_id") != val:
                        ok = False
                        break
                else:
                    # Treat any other key as a ts-range field. Apply $gte / $lt.
                    field_value = doc.get(key)
                    if not isinstance(val, dict):
                        if field_value != val:
                            ok = False
                            break
                        continue
                    if "$gte" in val and (
                        field_value is None or field_value < val["$gte"]
                    ):
                        ok = False
                        break
                    if "$lt" in val and (
                        field_value is None or field_value >= val["$lt"]
                    ):
                        ok = False
                        break
            if ok:
                results.append(dict(doc))
        return _Cursor(results)


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._sort: tuple[str, int] | None = None
        self._limit: int | None = None

    def sort(self, field: str, direction: int) -> _Cursor:
        self._sort = (field, direction)
        return self

    def limit(self, n: int) -> _Cursor:
        self._limit = n
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = list(self._docs)
        if self._sort is not None:
            field, direction = self._sort
            docs.sort(
                key=lambda d: d.get(field) or datetime.min.replace(tzinfo=UTC),
                reverse=direction == -1,
            )
        if self._limit is not None:
            docs = docs[: self._limit]
        if length is not None:
            docs = docs[:length]
        return docs


class _DbDouble:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self._collections = {
            name: _Collection(rows) for name, rows in collections.items()
        }

    def __getitem__(self, name: str) -> _Collection:
        # Unknown collections return an empty one rather than raising.
        return self._collections.get(name, _Collection([]))


def _mongo_adapter(
    collections: dict[str, list[dict[str, Any]]],
) -> MagicMock:
    adapter = MagicMock()
    adapter.db = _DbDouble(collections)
    return adapter


# ----------------------------------------------------------------------
# Repository tests.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_collections_returns_empty_events():
    repo = StrategyTimelineRepository(
        mysql_adapter=None, mongodb_adapter=_mongo_adapter({})
    )
    result = await repo.get_timeline(strategy_id="s1")
    assert result["events"] == []
    assert result["next_cursor"] is None
    assert result["strategy_id"] == "s1"


@pytest.mark.asyncio
async def test_merges_chronologically_across_sources():
    collections = {
        "strategy_config_audit": [
            {
                "strategy_id": "s1",
                "_id": "cfg1",
                "changed_at": T0 - timedelta(hours=3),
            },
        ],
        "characterizations": [
            {
                "strategy_id": "s1",
                "_id": "c1",
                "created_at": T0 - timedelta(hours=2),
            },
        ],
        "cio_decisions": [
            {
                "strategy_id": "s1",
                "decision_id": "d1",
                "timestamp": T0 - timedelta(hours=1),
            },
        ],
    }
    repo = StrategyTimelineRepository(
        mysql_adapter=None, mongodb_adapter=_mongo_adapter(collections)
    )
    result = await repo.get_timeline(strategy_id="s1")
    types = [e["type"] for e in result["events"]]
    assert types == ["config", "characterization", "decision"]


@pytest.mark.asyncio
async def test_filters_by_type_list():
    collections = {
        "strategy_config_audit": [
            {
                "strategy_id": "s1",
                "_id": "cfg1",
                "changed_at": T0 - timedelta(hours=3),
            },
        ],
        "characterizations": [
            {
                "strategy_id": "s1",
                "_id": "c1",
                "created_at": T0 - timedelta(hours=2),
            },
        ],
        "cio_decisions": [
            {
                "strategy_id": "s1",
                "decision_id": "d1",
                "timestamp": T0 - timedelta(hours=1),
            },
        ],
    }
    repo = StrategyTimelineRepository(
        mysql_adapter=None, mongodb_adapter=_mongo_adapter(collections)
    )
    result = await repo.get_timeline(
        strategy_id="s1", types=["characterization", "decision"]
    )
    types = [e["type"] for e in result["events"]]
    assert types == ["characterization", "decision"]


@pytest.mark.asyncio
async def test_unknown_type_in_filter_is_silently_dropped():
    repo = StrategyTimelineRepository(
        mysql_adapter=None,
        mongodb_adapter=_mongo_adapter(
            {
                "characterizations": [
                    {
                        "strategy_id": "s1",
                        "_id": "c1",
                        "created_at": T0 - timedelta(hours=2),
                    },
                ]
            }
        ),
    )
    result = await repo.get_timeline(
        strategy_id="s1", types=["characterization", "totally-fake"]
    )
    assert [e["type"] for e in result["events"]] == ["characterization"]


@pytest.mark.asyncio
async def test_isolates_to_requested_strategy_id():
    collections = {
        "characterizations": [
            {
                "strategy_id": "s1",
                "_id": "c1",
                "created_at": T0 - timedelta(hours=1),
            },
            {
                "strategy_id": "s2",
                "_id": "c2",
                "created_at": T0 - timedelta(hours=1),
            },
        ],
    }
    repo = StrategyTimelineRepository(
        mysql_adapter=None, mongodb_adapter=_mongo_adapter(collections)
    )
    result = await repo.get_timeline(strategy_id="s1")
    assert len(result["events"]) == 1
    assert result["events"][0]["payload"]["strategy_id"] == "s1"


@pytest.mark.asyncio
async def test_cursor_pagination_walks_in_order():
    rows = []
    for i in range(5):
        rows.append(
            {
                "strategy_id": "s1",
                "decision_id": f"d{i}",
                "timestamp": T0 - timedelta(minutes=10 - i),
            }
        )
    repo = StrategyTimelineRepository(
        mysql_adapter=None,
        mongodb_adapter=_mongo_adapter({"cio_decisions": rows}),
    )
    page1 = await repo.get_timeline(strategy_id="s1", limit=2)
    assert len(page1["events"]) == 2
    assert page1["next_cursor"] is not None
    seen_ids = {e["event_id"] for e in page1["events"]}

    page2 = await repo.get_timeline(
        strategy_id="s1", limit=2, cursor=page1["next_cursor"]
    )
    assert len(page2["events"]) == 2
    for e in page2["events"]:
        assert e["event_id"] not in seen_ids
    seen_ids.update(e["event_id"] for e in page2["events"])

    page3 = await repo.get_timeline(
        strategy_id="s1", limit=2, cursor=page2["next_cursor"]
    )
    # 5 total / 2 per page → last page has 1 event and no next cursor.
    assert len(page3["events"]) == 1
    assert page3["next_cursor"] is None
    seen_ids.add(page3["events"][0]["event_id"])
    assert seen_ids == {"d0", "d1", "d2", "d3", "d4"}


@pytest.mark.asyncio
async def test_malformed_cursor_falls_back_to_first_page():
    rows = [
        {
            "strategy_id": "s1",
            "decision_id": "d1",
            "timestamp": T0 - timedelta(minutes=5),
        }
    ]
    repo = StrategyTimelineRepository(
        mysql_adapter=None,
        mongodb_adapter=_mongo_adapter({"cio_decisions": rows}),
    )
    result = await repo.get_timeline(strategy_id="s1", cursor="not-a-real-cursor")
    # Malformed cursor decodes to (None, None) — caller still gets data.
    assert len(result["events"]) == 1


@pytest.mark.asyncio
async def test_missing_lifecycle_collection_is_silently_empty():
    """The strategy_lifecycle_events writer ships with P1.2 — until then,
    a missing collection must not break the timeline."""
    repo = StrategyTimelineRepository(
        mysql_adapter=None,
        mongodb_adapter=_mongo_adapter(
            {
                "characterizations": [
                    {
                        "strategy_id": "s1",
                        "_id": "c1",
                        "created_at": T0 - timedelta(hours=1),
                    },
                ]
            }
        ),
    )
    result = await repo.get_timeline(
        strategy_id="s1", types=["lifecycle", "characterization"]
    )
    assert [e["type"] for e in result["events"]] == ["characterization"]


@pytest.mark.asyncio
async def test_limit_is_clamped_to_max():
    rows = []
    for i in range(2000):
        rows.append(
            {
                "strategy_id": "s1",
                "decision_id": f"d{i:04d}",
                "timestamp": T0 - timedelta(seconds=2000 - i),
            }
        )
    repo = StrategyTimelineRepository(
        mysql_adapter=None,
        mongodb_adapter=_mongo_adapter({"cio_decisions": rows}),
    )
    result = await repo.get_timeline(strategy_id="s1", limit=999999)
    # The Mongo-side `.limit(limit)` cap is 1000, so the page returns at
    # most 1000 rows from any single source.
    assert len(result["events"]) <= 1000


# ----------------------------------------------------------------------
# API endpoint.
# ----------------------------------------------------------------------


@pytest.fixture()
def client_with_events():
    app = create_app()
    db_manager_stub = MagicMock()
    db_manager_stub.mongodb_adapter = _mongo_adapter(
        {
            "characterizations": [
                {
                    "strategy_id": "s1",
                    "_id": "c1",
                    "created_at": T0 - timedelta(hours=1),
                }
            ],
            "cio_decisions": [
                {
                    "strategy_id": "s1",
                    "decision_id": "d1",
                    "timestamp": T0 - timedelta(minutes=30),
                }
            ],
        }
    )
    db_manager_stub.mysql_adapter = None
    api_module.db_manager = db_manager_stub
    try:
        yield TestClient(app)
    finally:
        api_module.db_manager = None


def test_endpoint_returns_merged_events(client_with_events):
    r = client_with_events.get("/api/v1/strategies/s1/timeline")
    assert r.status_code == 200
    body = r.json()
    types = [e["type"] for e in body["events"]]
    assert types == ["characterization", "decision"]
    assert body["strategy_id"] == "s1"


def test_endpoint_honors_types_query(client_with_events):
    r = client_with_events.get(
        "/api/v1/strategies/s1/timeline", params={"types": "decision"}
    )
    assert r.status_code == 200
    body = r.json()
    assert [e["type"] for e in body["events"]] == ["decision"]


def test_endpoint_503_when_db_unavailable():
    app = create_app()
    api_module.db_manager = None
    client = TestClient(app)
    r = client.get("/api/v1/strategies/s1/timeline")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_event_provider_exception_falls_back_to_empty():
    """A failing adapter must not 500 the whole timeline page."""
    bad_adapter = MagicMock()
    bad_adapter.db = MagicMock()
    bad_adapter.db.__getitem__ = MagicMock(
        side_effect=Exception("collection unreachable")
    )
    repo = StrategyTimelineRepository(mysql_adapter=None, mongodb_adapter=bad_adapter)
    result = await repo.get_timeline(strategy_id="s1")
    # Every per-collection read failed; result is just empty rows.
    assert result["events"] == []
