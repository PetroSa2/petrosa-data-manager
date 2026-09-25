from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.lease_repository import LeaseRepository


def _repo(collection):
    return LeaseRepository(SimpleNamespace(db={"service_leases": collection}))


@pytest.mark.asyncio
async def test_acquire_uses_expiry_cas_and_upsert():
    now = datetime.now(UTC)
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(
            return_value={"name": "leader:dm", "owner": "pod-a", "expires_at": now, "fencing_token": 1}
        ),
        find_one=AsyncMock(),
    )
    result = await _repo(collection).acquire("leader:dm", "pod-a", 30)
    query, _update = collection.find_one_and_update.call_args.args
    assert result["acquired"] is True
    assert "$or" in query
    assert collection.find_one_and_update.call_args.kwargs["upsert"] is True


@pytest.mark.asyncio
async def test_duplicate_acquire_returns_current_holder():
    now = datetime.now(UTC)
    holder = {"name": "x", "owner": "pod-a", "expires_at": now, "fencing_token": 4}
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(side_effect=DuplicateKeyError("duplicate")),
        find_one=AsyncMock(return_value=holder),
    )
    result = await _repo(collection).acquire("x", "pod-b", 10)
    assert result == {"acquired": False, **holder, "acquired_at": None, "renewed_at": None}


@pytest.mark.asyncio
async def test_renew_non_owner_returns_none_and_release_filters_owner():
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(return_value=None),
        delete_one=AsyncMock(return_value=SimpleNamespace(deleted_count=1)),
    )
    repo = _repo(collection)
    assert await repo.renew("x", "pod-b", 10) is None
    assert await repo.release("x", "pod-b") is True
    assert collection.delete_one.call_args.args[0] == {"name": "x", "owner": "pod-b"}
