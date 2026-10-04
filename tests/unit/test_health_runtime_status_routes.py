"""Regression tests for runtime component health status wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes.health import audit_status, leader_status


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
