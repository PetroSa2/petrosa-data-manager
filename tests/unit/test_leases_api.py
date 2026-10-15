import asyncio
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import data_manager.api.routes.analysis as analysis
from data_manager.api.routes import leases
from data_manager.db.repositories.lease_repository import LeaseRepository


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


def test_lease_repository_prefers_isolated_database():
    isolated = {"service_leases": object()}
    shared = {"service_leases": object()}
    repository = LeaseRepository(SimpleNamespace(lease_db=isolated, db=shared))

    assert repository._collection() is isolated["service_leases"]


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
    assert any(
        record["lease_error_class"] == "renew_failed"
        for record in leases._lease_diagnostics
    )


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
    timing = json.loads(next(record.message for record in caplog.records))
    assert timing["event"] == "lease_timing"
    assert timing["lease_op"] == "get"
    assert timing["lease_queue_wait_ms"] >= 5
    assert timing["lease_handler_ms"] >= 0
    assert timing["lease_mongo_wait_ms"] >= 0
    assert timing["lease_outcome"] == "success"
    assert timing["lease_error_class"] == "success"
    assert "lease_mongo_readiness" in timing
    assert "lease_mysql_backend" in timing
    assert "lease_event_loop_state" in timing


@pytest.mark.asyncio
async def test_timed_out_lease_logs_bounded_backend_diagnostics(caplog):
    request = SimpleNamespace(state=SimpleNamespace(received_at=perf_counter()))

    async def action():
        raise TimeoutError("backend timeout")

    with (
        caplog.at_level(logging.INFO, logger=leases.__name__),
        pytest.raises(HTTPException),
    ):
        await leases._run(request, "renew", action)

    timing = json.loads(next(record.message for record in caplog.records))
    assert timing["lease_op"] == "renew"
    assert timing["lease_queue_wait_ms"] >= 0
    assert timing["lease_handler_ms"] >= 0
    assert timing["lease_mongo_wait_ms"] >= 0
    assert timing["lease_outcome"] == "error"
    assert timing["lease_error_class"] == "timeout"
    assert timing["lease_mongo_readiness"] == "failed"


@pytest.mark.asyncio
async def test_unready_backend_logs_readiness_stall(caplog):
    request = SimpleNamespace(state=SimpleNamespace(received_at=perf_counter()))

    async def action():
        raise RuntimeError("MongoDB unavailable")

    with (
        caplog.at_level(logging.INFO, logger=leases.__name__),
        pytest.raises(HTTPException),
    ):
        await leases._run(request, "get", action)

    timing = json.loads(next(record.message for record in caplog.records))
    assert timing["lease_outcome"] == "error"
    assert timing["lease_error_class"] == "readiness_failure"
    assert timing["lease_mongo_readiness"] == "failed"


@pytest.mark.asyncio
async def test_backend_failure_is_queryable(caplog):
    request = SimpleNamespace(state=SimpleNamespace(received_at=perf_counter()))

    async def action():
        raise ValueError("backend rejected request")

    with (
        caplog.at_level(logging.INFO, logger=leases.__name__),
        pytest.raises(HTTPException),
    ):
        await leases._run(request, "release", action)

    timing = json.loads(next(record.message for record in caplog.records))
    assert timing["lease_error_class"] == "backend_failure"
    assert timing["lease_mysql_backend"] in {"unknown", "healthy", "unavailable"}


@pytest.mark.asyncio
async def test_lease_diagnostics_query_returns_bounded_summary(caplog):
    leases._lease_diagnostics.clear()
    request = SimpleNamespace(state=SimpleNamespace(received_at=perf_counter()))

    async def action():
        return "lease"

    with caplog.at_level(logging.INFO, logger=leases.__name__):
        await leases._run(request, "get", action)
    result = await leases.diagnostics(operation="get", outcome="success")

    assert result["count"] == 1
    summary = next(iter(result["summary"].values()))
    assert summary["queue_wait_ms"]["count"] == 1
    assert summary["handler_ms"]["p95"] >= 0

    with pytest.raises(HTTPException) as invalid:
        await leases.diagnostics(
            since=datetime.now(UTC), until=datetime.now(UTC) - timedelta(seconds=1)
        )
    assert invalid.value.status_code == 422


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
