import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import data_manager.api.routes.analysis as analysis
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
    result = await leases.acquire(
        "leader:data-manager", leases.LeaseRequest(owner="pod-a", ttl_seconds=30)
    )
    assert result["acquired"] is True
    assert result["fencing_token"] == 1


@pytest.mark.asyncio
async def test_bad_name_and_renew_loss_are_rejected():
    with pytest.raises(HTTPException) as bad_name:
        await leases.acquire(
            "bad/name", leases.LeaseRequest(owner="pod-a", ttl_seconds=30)
        )
    assert bad_name.value.status_code == 422
    with pytest.raises(HTTPException) as lost:
        await leases.renew(
            "leader:data-manager", leases.LeaseRequest(owner="pod-b", ttl_seconds=30)
        )
    assert lost.value.status_code == 409


@pytest.mark.asyncio
async def test_release_and_unknown_get():
    assert await leases.release(
        "leader:data-manager", leases.ReleaseRequest(owner="pod-a")
    ) == {"released": True}
    with pytest.raises(HTTPException) as missing:
        await leases.get("leader:data-manager")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_lease_timing_logs_queue_wait_and_handler_time(caplog):
    request = SimpleNamespace(state=SimpleNamespace(received_at=perf_counter() - 0.01))
    with caplog.at_level(logging.INFO, logger=leases.__name__):

        async def action():
            return "lease"

        result = await leases._run(request, "get", action)

    assert result == "lease"
    timing = next(
        record for record in caplog.records if record.message == "lease timing"
    )
    assert timing.lease_op == "get"
    assert timing.lease_queue_wait_ms >= 5
    assert timing.lease_handler_ms >= 0


@pytest.mark.asyncio
async def test_lease_get_and_renew_are_not_blocked_by_heavy_analytics(monkeypatch):
    lease = {
        "owner": "pod-a",
        "expires_at": datetime.now(UTC) + timedelta(seconds=30),
        "fencing_token": 7,
    }

    class ResponsiveRepository:
        async def get(self, _name):
            return lease

        async def renew(self, _name, _owner, _ttl):
            return lease

    monkeypatch.setattr(leases, "_repo", lambda: ResponsiveRepository())
    workers = analysis._ANALYTICS_EXECUTOR_WORKERS
    assert workers >= 2
    barrier = threading.Barrier(workers + 1)
    release = threading.Event()
    thread_names = []

    def long_analytics_job():
        thread_names.append(threading.current_thread().name)
        barrier.wait(timeout=2)
        release.wait(timeout=2)

    futures = [
        asyncio.create_task(analysis._run_heavy(long_analytics_job))
        for _ in range(workers)
    ]
    await asyncio.to_thread(barrier.wait, 1)
    try:
        assert all(name.startswith("data-manager-analytics") for name in thread_names)
        started = perf_counter()
        result = await asyncio.gather(
            leases.get("leader:data-manager"),
            leases.renew(
                "leader:data-manager",
                leases.LeaseRequest(owner="pod-a", ttl_seconds=30),
            ),
        )
        assert perf_counter() - started < 1
        assert result[0]["owner"] == "pod-a"
        assert result[1]["renewed"] is True
    finally:
        release.set()
        await asyncio.gather(*futures)
