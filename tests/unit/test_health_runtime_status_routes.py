"""Regression tests for runtime component health status wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import constants
import data_manager.api.app as api_module
import data_manager.auditor.scheduler as scheduler_module
import data_manager.services.backfill_trigger as backfill_trigger_module
from data_manager.api.routes.health import audit_status, leader_status
from data_manager.main import DataManagerApp


@pytest.fixture(autouse=True)
def reset_runtime_references():
    original_leader_election = api_module.leader_election
    original_audit_scheduler = api_module.audit_scheduler
    yield
    api_module.leader_election = original_leader_election
    api_module.audit_scheduler = original_audit_scheduler


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_health_routes_report_wired_components():
    leader = MagicMock()
    leader.get_status.return_value = {
        "pod_id": "pod-1",
        "is_leader": True,
        "leader_pod_id": "pod-1",
        "running": True,
        "heartbeat_interval": 10,
        "election_timeout": 30,
    }
    scheduler = MagicMock()
    scheduler.get_status.return_value = {
        "running": True,
        "last_audit_time": "2026-10-04T02:28:00+00:00",
        "is_leader": True,
        "leader_pod_id": "pod-1",
        "pod_id": "pod-1",
    }
    api_module.leader_election = leader
    api_module.audit_scheduler = scheduler

    leader_response = await leader_status()
    audit_response = await audit_status()

    assert leader_response["enabled"] is True
    assert leader_response["is_leader"] is True
    assert audit_response["enabled"] is True
    assert audit_response["running"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_health_routes_report_unwired_components():
    api_module.leader_election = None
    api_module.audit_scheduler = None

    leader_response = await leader_status()
    audit_response = await audit_status()

    assert leader_response["enabled"] is False
    assert leader_response["message"] == "Leader election not initialized or disabled"
    assert audit_response["enabled"] is False
    assert audit_response["message"] == "Audit scheduler not initialized or disabled"


@pytest.mark.unit
def test_runtime_component_references_are_wired_to_api_module():
    app = DataManagerApp()
    leader = object()
    scheduler = object()

    app._set_leader_election_health_reference(leader)
    app._set_audit_scheduler_health_reference(scheduler)

    assert api_module.leader_election is leader
    assert api_module.audit_scheduler is scheduler


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auditor_start_wires_scheduler_reference(monkeypatch):
    scheduler = MagicMock()
    scheduler.get_status.return_value = {
        "running": True,
        "is_leader": True,
        "leader_pod_id": "pod-1",
    }
    start_called = asyncio.Event()
    release_start = asyncio.Event()

    async def start_scheduler():
        start_called.set()
        await release_start.wait()

    scheduler.start = start_scheduler
    trigger = MagicMock()
    trigger.start = AsyncMock()
    trigger.stop = AsyncMock()
    monkeypatch.setattr(
        scheduler_module, "AuditScheduler", MagicMock(return_value=scheduler)
    )
    monkeypatch.setattr(
        backfill_trigger_module,
        "BackfillTrigger",
        MagicMock(return_value=trigger),
    )
    monkeypatch.setattr(constants, "ENABLE_AUDITOR", True)

    app = DataManagerApp()
    app.db_manager = MagicMock()
    app.db_manager.mongo_healthy.return_value = True
    app.backfill_queue = MagicMock()

    auditor_task = asyncio.create_task(app._run_auditor())
    await asyncio.wait_for(start_called.wait(), timeout=1)

    assert api_module.audit_scheduler is scheduler
    audit_response = await audit_status()
    assert audit_response["enabled"] is True
    assert audit_response["is_leader"] is True

    release_start.set()
    await auditor_task
    assert api_module.audit_scheduler is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auditor_clears_scheduler_reference_when_start_fails(monkeypatch):
    scheduler = MagicMock()
    scheduler.start = AsyncMock(side_effect=RuntimeError("scheduler failed"))
    trigger = MagicMock()
    trigger.start = AsyncMock()
    trigger.stop = AsyncMock()
    monkeypatch.setattr(
        scheduler_module, "AuditScheduler", MagicMock(return_value=scheduler)
    )
    monkeypatch.setattr(
        backfill_trigger_module,
        "BackfillTrigger",
        MagicMock(return_value=trigger),
    )
    monkeypatch.setattr(constants, "ENABLE_AUDITOR", True)

    app = DataManagerApp()
    app.db_manager = MagicMock()
    app.db_manager.mongo_healthy.return_value = True
    app.backfill_queue = MagicMock()

    await app._run_auditor()

    assert api_module.audit_scheduler is None
