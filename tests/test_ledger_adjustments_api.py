from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.routes import ledger as module
from data_manager.db.repositories.ledger_adjustments_repository import (
    AdjustmentConflictError,
    AdjustmentValidationError,
)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def _valid_adjustment_payload():
    return {
        "adjustment_id": "dry-run-1",
        "applied_at": datetime.now(UTC).isoformat(),
        "applied_by": "agent",
        "approved_by": "reviewer",
        "target_table": "positions",
        "target_key": "1",
        "before": {"status": "open", "close_reason": None, "pnl_unknown": False},
        "after": {
            "status": "superseded",
            "close_reason": "phantom_superseded",
            "pnl_unknown": True,
        },
        "reason_code": "phantom_superseded",
        "evidence_ref": "issue-467",
        "run_mode": "dry_run",
        "dry_run_adjustment_id": None,
    }


def test_adjustment_insert_requires_all_fields(client):
    payload = _valid_adjustment_payload()
    payload.pop("approved_by")
    response = client.post("/api/v1/ledger/adjustments", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_adjustment_maps_validation_errors(monkeypatch):
    class Repo:
        def insert_adjustment(self, payload):
            raise AdjustmentValidationError("invalid")

    monkeypatch.setattr(module, "_adjustments_repo", lambda: Repo())
    with pytest.raises(HTTPException) as error:
        await module.post_adjustment(
            module.LedgerAdjustmentWrite(**_valid_adjustment_payload())
        )
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_get_adjustments_rejects_invalid_range():
    with pytest.raises(HTTPException) as error:
        await module.get_adjustments(
            from_=datetime(2026, 1, 2, tzinfo=UTC),
            to=datetime(2026, 1, 1, tzinfo=UTC),
        )
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_supersede_route_does_not_require_mongo(monkeypatch):
    class GuardedManager:
        mysql_adapter = object()

        @property
        def mongodb_adapter(self):
            raise AssertionError("MongoDB must not be accessed")

    api_module.db_manager = GuardedManager()

    captured = {}

    def fake_supersede(self, payload):
        captured["payload"] = payload
        return {"id": payload["id"], "status": "superseded"}

    monkeypatch.setattr(
        module.LedgerAdjustmentsRepository, "supersede_position", fake_supersede
    )
    body = module.PositionSupersedeRequest(
        id=1,
        expected_before={"status": "open"},
        reason_code="phantom_superseded",
        evidence_ref="issue-467",
        applied_by="agent",
        approved_by="reviewer",
        dry_run_adjustment_id="dry-run-1",
    )
    result = await module.post_positions_supersede(body)
    assert result["status"] == "superseded"
    assert "adjustment_id" in captured["payload"]
    assert "applied_at" in captured["payload"]
    api_module.db_manager = None


@pytest.mark.asyncio
async def test_supersede_maps_conflicts(monkeypatch):
    class Repo:
        def supersede_position(self, payload):
            raise AdjustmentConflictError("drift")

    monkeypatch.setattr(module, "_adjustments_repo", lambda: Repo())
    body = module.PositionSupersedeRequest(
        id=1,
        expected_before={"status": "open"},
        reason_code="phantom_superseded",
        evidence_ref="issue-467",
        applied_by="agent",
        approved_by="reviewer",
        dry_run_adjustment_id="dry-run-1",
    )
    with pytest.raises(HTTPException) as error:
        await module.post_positions_supersede(body)
    assert error.value.status_code == 409
