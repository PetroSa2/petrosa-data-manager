"""Tests for ``GET /api/dashboard/envelope-authorship`` (P4.6-AC4.a / #203).

Backend API surface that powers the operator dashboard envelope-authorship
pane — returns the current active envelope per ``strategy_or_portfolio_key``,
pending changes awaiting operator action, and the decided-history tail
(paginated). API-only — no SPA changes here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.models.envelope import Envelope
from data_manager.models.envelope_change import (
    EnvelopeChangeResolution,
    PendingEnvelopeChange,
)


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Boot the FastAPI app with a fake mongodb_adapter wired in.

    The route uses ``api_module.db_manager.mongodb_adapter`` via
    ``_require_mongo`` — we just need that attribute to be truthy.
    """
    fake_db_manager = MagicMock()
    fake_db_manager.mongodb_adapter = MagicMock()
    monkeypatch.setattr(api_module, "db_manager", fake_db_manager)
    app = create_app()
    return TestClient(app)


def _envelope(key: str, version: int, source: str = "operator_approved") -> Envelope:
    return Envelope(
        envelope_id=f"env-{key}-{version}",
        strategy_or_portfolio_key=key,
        version=version,
        source=source,
        value={"max_drawdown_pct": 5.0 + version},
        originating_characterization_revision="rev-abc",
        operator_id="alice" if source == "operator_approved" else None,
        created_at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
        signed_action_id="sa-1",
    )


def _pending_change(key: str, change_id: str) -> PendingEnvelopeChange:
    return PendingEnvelopeChange(
        change_id=change_id,
        strategy_or_portfolio_key=key,
        proposed_envelope_value={"max_drawdown_pct": 8.0},
        originating_characterization_revision="rev-pending",
        created_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
        status="pending",
    )


def _decided_change(
    key: str, change_id: str, decided_at: datetime, status: str = "accepted"
) -> PendingEnvelopeChange:
    return PendingEnvelopeChange(
        change_id=change_id,
        strategy_or_portfolio_key=key,
        proposed_envelope_value={"max_drawdown_pct": 6.5},
        originating_characterization_revision="rev-decided",
        created_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
        status=status,
        resolution=EnvelopeChangeResolution(
            operator_id="alice",
            decided_at=decided_at,
            signed_action_id="sa-decision-1",
            rejection_reason="not viable" if status == "rejected" else None,
        ),
    )


def _patch_repos(monkeypatch, *, current, pending, history, next_cursor=None):
    """Stub EnvelopeRepository.list_active_envelopes + PendingEnvelopeChangeRepository.{list_pending,list_decided}."""
    monkeypatch.setattr(
        "data_manager.api.routes.dashboard.EnvelopeRepository.list_active_envelopes"
        if False
        else "data_manager.db.repositories.envelope_repository.EnvelopeRepository.list_active_envelopes",
        AsyncMock(return_value=current),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_pending",
        AsyncMock(return_value=pending),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_decided",
        AsyncMock(return_value=(history, next_cursor)),
    )


# ---------------------------------------------------------------------------
# AC4.a — happy path
# ---------------------------------------------------------------------------


def test_returns_empty_buckets_when_no_data(client: TestClient, monkeypatch) -> None:
    _patch_repos(monkeypatch, current=[], pending=[], history=[])
    response = client.get("/api/dashboard/envelope-authorship")
    assert response.status_code == 200
    body = response.json()
    assert body["current"] == []
    assert body["pending"] == []
    assert body["history"] == []
    assert body["next_cursor"] is None
    assert body["filters"] == {"key": None, "limit": 50}


def test_returns_all_three_buckets_populated(client: TestClient, monkeypatch) -> None:
    current = [_envelope("strategy:momentum-v3", version=5)]
    pending = [_pending_change("strategy:momentum-v3", "ch-pending-1")]
    history = [
        _decided_change(
            "strategy:momentum-v3",
            "ch-decided-1",
            datetime(2026, 6, 1, 11, 0, 0, tzinfo=UTC),
        )
    ]
    _patch_repos(monkeypatch, current=current, pending=pending, history=history)

    response = client.get("/api/dashboard/envelope-authorship")
    assert response.status_code == 200
    body = response.json()
    assert len(body["current"]) == 1
    assert body["current"][0]["version"] == 5
    assert body["current"][0]["source"] == "operator_approved"
    assert len(body["pending"]) == 1
    assert body["pending"][0]["change_id"] == "ch-pending-1"
    assert body["pending"][0]["status"] == "pending"
    assert len(body["history"]) == 1
    assert body["history"][0]["change_id"] == "ch-decided-1"
    assert body["history"][0]["status"] == "accepted"


# ---------------------------------------------------------------------------
# AC4.a.1 — key filter
# ---------------------------------------------------------------------------


def test_key_filter_passed_through_to_repos(client: TestClient, monkeypatch) -> None:
    """The ``?key=`` query param must be forwarded to all three repo calls so
    each bucket gets narrowed to the same key (no cross-bucket drift)."""
    captured: dict[str, Any] = {}

    async def _fake_list_active(self, *, key=None, limit=200):
        captured["current_key"] = key
        return [_envelope("strategy:momentum-v3", version=3)]

    async def _fake_list_pending(self, limit=200):
        # list_pending doesn't take a key arg — the route filters in Python.
        return [_pending_change("strategy:momentum-v3", "ch-pending-1")]

    async def _fake_list_decided(self, *, key=None, limit=50, cursor=None):
        captured["history_key"] = key
        captured["history_limit"] = limit
        return [], None

    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.list_active_envelopes",
        _fake_list_active,
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_pending",
        _fake_list_pending,
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_decided",
        _fake_list_decided,
    )

    response = client.get("/api/dashboard/envelope-authorship?key=strategy:momentum-v3")
    assert response.status_code == 200
    assert captured == {
        "current_key": "strategy:momentum-v3",
        "history_key": "strategy:momentum-v3",
        "history_limit": 50,
    }
    body = response.json()
    assert body["filters"] == {"key": "strategy:momentum-v3", "limit": 50}


# ---------------------------------------------------------------------------
# Pagination cursor round-trip
# ---------------------------------------------------------------------------


def test_history_pagination_returns_next_cursor(
    client: TestClient, monkeypatch
) -> None:
    decided_at = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
    history = [_decided_change("k", f"ch-{i}", decided_at) for i in range(3)]
    next_cursor = decided_at.isoformat()
    _patch_repos(
        monkeypatch,
        current=[],
        pending=[],
        history=history,
        next_cursor=next_cursor,
    )

    response = client.get("/api/dashboard/envelope-authorship?limit=3")
    body = response.json()
    assert body["next_cursor"] == next_cursor
    assert body["filters"]["limit"] == 3


def test_history_cursor_round_trip(client: TestClient, monkeypatch) -> None:
    """Caller passes the previous page's next_cursor back as ``cursor`` to
    get older rows — the value flows through the route to the repo."""
    captured: dict[str, Any] = {}

    async def _fake_list_decided(self, *, key=None, limit=50, cursor=None):
        captured["cursor"] = cursor
        return [], None

    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.list_active_envelopes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_pending",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_decided",
        _fake_list_decided,
    )

    response = client.get(
        "/api/dashboard/envelope-authorship",
        params={"cursor": "2026-06-01T08:00:00+00:00"},
    )
    assert response.status_code == 200
    assert captured["cursor"] == "2026-06-01T08:00:00+00:00"


def test_bad_cursor_returns_400(client: TestClient, monkeypatch) -> None:
    """A malformed cursor surfaces as RFC-7807 400, not 500. The repo raises
    ValueError; the route maps it to ``invalid_cursor`` title."""

    async def _fake_list_decided(self, *, key=None, limit=50, cursor=None):
        raise ValueError("cursor must be an ISO-8601 datetime; got 'banana'")

    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.list_active_envelopes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_pending",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_decided",
        _fake_list_decided,
    )

    response = client.get("/api/dashboard/envelope-authorship?cursor=banana")
    assert response.status_code == 400
    assert response.json()["detail"]["title"] == "invalid_cursor"


def test_limit_clamped_to_max_200(client: TestClient, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_list_decided(self, *, key=None, limit=50, cursor=None):
        captured["limit"] = limit
        return [], None

    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.list_active_envelopes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_pending",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.pending_envelope_change_repository.PendingEnvelopeChangeRepository.list_decided",
        _fake_list_decided,
    )

    response = client.get("/api/dashboard/envelope-authorship?limit=999")
    body = response.json()
    assert response.status_code == 200
    assert captured["limit"] == 200
    assert body["filters"]["limit"] == 200


# ---------------------------------------------------------------------------
# 503 on Mongo unavailability
# ---------------------------------------------------------------------------


def test_503_when_mongo_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "db_manager", None)
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/dashboard/envelope-authorship")
    assert response.status_code == 503
    assert response.json()["detail"]["title"] == "database_unavailable"
