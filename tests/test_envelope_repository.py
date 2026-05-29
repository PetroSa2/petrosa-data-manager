"""Tests for EnvelopeRepository (#188, P4.6-AC2 / FR62)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.envelope_repository import (
    ENVELOPES_COLLECTION,
    EnvelopeRepository,
)
from data_manager.models.envelope import Envelope


def _build_envelope(
    key: str = "strategy:btc_momentum_v3",
    *,
    version: int = 1,
    source: str = "operator_approved",
) -> Envelope:
    return Envelope(
        envelope_id=f"{key}:v{version}",
        version=version,
        strategy_or_portfolio_key=key,
        value={"max_drawdown_pct": 5.0, "max_position_size": 25_000},
        source=source,  # type: ignore[arg-type]
        originating_characterization_revision=None,
        operator_id="op-1" if source == "operator_approved" else None,
        signed_action_id="signed-action-abc123",
    )


class _StubCursor:
    """Async iterator stub for ``find().sort().limit()`` chains."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def sort(self, *_args: Any, **_kwargs: Any) -> _StubCursor:
        return self

    def limit(self, _n: int) -> _StubCursor:
        return self

    def __aiter__(self) -> _StubCursor:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


@pytest.fixture
def mock_mongo():
    """A MagicMock that exposes ``db.envelopes`` with AsyncMock'd operations."""
    mongo = MagicMock()
    mongo.is_connected = True
    mongo.db = MagicMock()
    mongo.db[ENVELOPES_COLLECTION] = MagicMock()
    col = mongo.db[ENVELOPES_COLLECTION]
    col.find_one = AsyncMock(return_value=None)
    col.insert_one = AsyncMock(return_value=None)
    col.create_index = AsyncMock(return_value=None)
    col.find = MagicMock(return_value=_StubCursor([]))
    return mongo


@pytest.mark.asyncio
async def test_ensure_indexes_creates_compound_index(mock_mongo):
    """AC2.d — composite (strategy_or_portfolio_key, version DESC) index is created."""
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)
    await repo.ensure_indexes()
    mock_mongo.db[ENVELOPES_COLLECTION].create_index.assert_awaited_once()
    args, kwargs = mock_mongo.db[ENVELOPES_COLLECTION].create_index.call_args
    assert args[0] == [("strategy_or_portfolio_key", 1), ("version", -1)]
    assert kwargs["name"] == "strategy_or_portfolio_key_1_version_-1"


@pytest.mark.asyncio
async def test_get_active_envelope_returns_none_when_empty(mock_mongo):
    """AC2.e — get_active_envelope returns None for an unknown key."""
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)
    mock_mongo.db[ENVELOPES_COLLECTION].find_one.return_value = None
    result = await repo.get_active_envelope("strategy:unknown")
    assert result is None


@pytest.mark.asyncio
async def test_get_active_envelope_returns_latest(mock_mongo):
    """AC2.e — get_active_envelope returns (version, value, source) for the latest doc."""
    envelope = _build_envelope(version=7, source="operator_approved")
    stored = envelope.model_dump(mode="json")
    stored["_id"] = envelope.doc_id()
    mock_mongo.db[ENVELOPES_COLLECTION].find_one.return_value = stored

    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)
    result = await repo.get_active_envelope("strategy:btc_momentum_v3")

    assert result is not None
    version, value, source = result
    assert version == 7
    assert value["max_drawdown_pct"] == 5.0
    assert source == "operator_approved"

    # The query MUST sort version desc — that's the contract that keeps
    # AC2.b (monotonic versioning) intact for reads.
    sort_arg = mock_mongo.db[ENVELOPES_COLLECTION].find_one.call_args.kwargs["sort"]
    assert sort_arg == [("version", -1)]


@pytest.mark.asyncio
async def test_insert_next_version_stamps_v1_on_empty_key(mock_mongo):
    """AC2.b — first insert for a new key gets version=1, deterministic doc_id."""
    mock_mongo.db[ENVELOPES_COLLECTION].find_one.return_value = None
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)

    candidate = _build_envelope(
        key="strategy:foo", version=999
    )  # version pre-write is ignored
    result = await repo.insert_next_version(candidate)

    assert result.version == 1
    assert result.envelope_id == "strategy:foo:v1"
    assert result.doc_id() == "strategy:foo:v1"

    # Persisted doc carries `_id` matching doc_id()
    inserted_doc = mock_mongo.db[ENVELOPES_COLLECTION].insert_one.call_args.args[0]
    assert inserted_doc["_id"] == "strategy:foo:v1"
    assert inserted_doc["version"] == 1


@pytest.mark.asyncio
async def test_insert_next_version_increments_for_existing_key(mock_mongo):
    """AC2.b — subsequent insert for an existing key gets latest+1."""
    mock_mongo.db[ENVELOPES_COLLECTION].find_one.return_value = {"version": 3}
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)

    candidate = _build_envelope(key="strategy:foo", version=1)
    result = await repo.insert_next_version(candidate)

    assert result.version == 4
    assert result.doc_id() == "strategy:foo:v4"


@pytest.mark.asyncio
async def test_insert_next_version_retries_on_duplicate_key(mock_mongo):
    """AC2.b — racing writer that lost the v<n> insert retries at v<n+1>."""
    # First find returns version=2, second returns version=3 (the racer landed at v3)
    mock_mongo.db[ENVELOPES_COLLECTION].find_one.side_effect = [
        {"version": 2},  # our compute → 3
        {"version": 3},  # after dup error, recompute → 4
    ]
    mock_mongo.db[ENVELOPES_COLLECTION].insert_one.side_effect = [
        DuplicateKeyError("dup v3"),  # racer won v3
        None,  # we win v4
    ]
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)

    result = await repo.insert_next_version(_build_envelope(key="strategy:foo"))

    assert result.version == 4
    assert mock_mongo.db[ENVELOPES_COLLECTION].insert_one.await_count == 2


@pytest.mark.asyncio
async def test_seed_legacy_skips_existing(mock_mongo):
    """AC2.f — migration skips keys that already have at least one version."""
    # First key already exists; second key is fresh.
    mock_mongo.db[ENVELOPES_COLLECTION].find_one.side_effect = [
        {"_id": "strategy:already:v5"},  # exists
        None,  # fresh
    ]
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)

    seeded = await repo.seed_legacy_characterization_envelopes(
        [
            ("strategy:already", {"a": 1}, "char-rev-1", "signed-1"),
            ("strategy:fresh", {"b": 2}, "char-rev-2", "signed-2"),
        ]
    )

    assert seeded == 1
    inserted_doc = mock_mongo.db[ENVELOPES_COLLECTION].insert_one.call_args.args[0]
    assert inserted_doc["strategy_or_portfolio_key"] == "strategy:fresh"
    assert inserted_doc["source"] == "characterization"
    assert inserted_doc["version"] == 1
    assert inserted_doc["_id"] == "strategy:fresh:v1"


@pytest.mark.asyncio
async def test_list_versions_returns_descending(mock_mongo):
    """list_versions returns version ints in descending order for an operator history pane."""
    mock_mongo.db[ENVELOPES_COLLECTION].find.return_value = _StubCursor(
        [{"version": 9}, {"version": 8}, {"version": 7}]
    )
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)

    versions = await repo.list_versions("strategy:foo", limit=3)

    assert versions == [9, 8, 7]


@pytest.mark.asyncio
async def test_envelope_repository_has_no_update_or_delete(mock_mongo):
    """AC2.c — append-only contract: no UPDATE or DELETE method exists on the public API."""
    repo = EnvelopeRepository(mongodb_adapter=mock_mongo)
    public_methods = {
        name
        for name in dir(repo)
        if not name.startswith("_") and callable(getattr(repo, name))
    }
    # The presence of these methods would break AC2.c. Explicit deny list keeps
    # future refactors honest.
    for forbidden in ("update_envelope", "delete_envelope", "update_one", "delete_one"):
        assert forbidden not in public_methods, (
            f"AC2.c violation: {forbidden!r} would mutate envelope history."
        )
