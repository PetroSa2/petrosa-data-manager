from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from data_manager.api.routes import leases


class FakeRepository:
    async def acquire(self, name, owner, ttl):
        return {
            "acquired": True,
            "name": name,
            "owner": owner,
            "expires_at": datetime.now(UTC) + timedelta(seconds=ttl),
            "fencing_token": 1,
        }

    async def renew(self, name, owner, ttl):
        return None

    async def release(self, name, owner):
        return True

    async def get(self, name):
        return None


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    monkeypatch.setattr(leases, "_repo", lambda: FakeRepository())


@pytest.mark.asyncio
async def test_acquire_returns_fencing_token():
    result = await leases.acquire("leader:data-manager", leases.LeaseRequest(owner="pod-a", ttl_seconds=30))
    assert result["acquired"] is True
    assert result["fencing_token"] == 1


@pytest.mark.asyncio
async def test_bad_name_and_renew_loss_are_rejected():
    with pytest.raises(HTTPException) as bad_name:
        await leases.acquire("bad/name", leases.LeaseRequest(owner="pod-a", ttl_seconds=30))
    assert bad_name.value.status_code == 422
    with pytest.raises(HTTPException) as lost:
        await leases.renew("leader:data-manager", leases.LeaseRequest(owner="pod-b", ttl_seconds=30))
    assert lost.value.status_code == 409


@pytest.mark.asyncio
async def test_release_and_unknown_get():
    assert await leases.release("leader:data-manager", leases.ReleaseRequest(owner="pod-a")) == {"released": True}
    with pytest.raises(HTTPException) as missing:
        await leases.get("leader:data-manager")
    assert missing.value.status_code == 404
