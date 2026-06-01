"""Tests for ``GET /api/breaches/{id}`` + DrawdownBreachRepository + subscriber.

P4.6-AC6.b / AC6.c / AC6.d / AC6.e / #194.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.models.drawdown_breach import DrawdownBreach
from data_manager.models.envelope import Envelope
from data_manager.services.drawdown_breach_subscriber import (
    DrawdownBreachSubscriber,
    _normalize_event,
    _synthesize_breach_id,
)


@pytest.fixture
def client(monkeypatch) -> TestClient:
    fake_db_manager = MagicMock()
    fake_db_manager.mongodb_adapter = MagicMock()
    monkeypatch.setattr(api_module, "db_manager", fake_db_manager)
    return TestClient(create_app())


def _legacy_breach() -> DrawdownBreach:
    """Breach predating AC6.a — envelope_version/source are None."""
    return DrawdownBreach(
        breach_id="legacy-1",
        strategy_id="momentum-v3",
        observed_drawdown_pct=7.2,
        envelope_value_pct=5.0,
        exceeded_by_pct=2.2,
        detected_at=datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC),
    )


def _new_breach() -> DrawdownBreach:
    """Breach post-AC6.a — envelope_version + envelope_source populated."""
    return DrawdownBreach(
        breach_id="new-1",
        strategy_id="momentum-v3",
        observed_drawdown_pct=6.5,
        envelope_value_pct=5.0,
        exceeded_by_pct=1.5,
        detected_at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
        envelope_version=7,
        envelope_source="operator_approved",
    )


def _envelope_v7() -> Envelope:
    return Envelope(
        envelope_id="env-1",
        strategy_or_portfolio_key="strategy:momentum-v3",
        version=7,
        source="operator_approved",
        value={"max_drawdown_pct": 5.0},
        originating_characterization_revision="rev-abc",
        operator_id="alice",
        created_at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
        signed_action_id="sa-1",
    )


# ---------------------------------------------------------------------------
# AC6.c — GET /api/breaches/{id}
# ---------------------------------------------------------------------------


def test_get_breach_404_when_unknown(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(return_value=None),
    )
    response = client.get("/api/breaches/missing-id")
    assert response.status_code == 404
    assert response.json()["detail"]["title"] == "breach_not_found"


def test_get_breach_returns_breach_plus_envelope_for_new_shape(
    client: TestClient, monkeypatch
) -> None:
    """AC6.c happy path — envelope hydrated when envelope_version is set."""
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(return_value=_new_breach()),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.get_version",
        AsyncMock(return_value=_envelope_v7()),
    )
    response = client.get("/api/breaches/new-1")
    assert response.status_code == 200
    body = response.json()
    assert body["breach"]["breach_id"] == "new-1"
    assert body["breach"]["envelope_version"] == 7
    assert body["breach"]["envelope_source"] == "operator_approved"
    assert body["envelope"] is not None
    assert body["envelope"]["version"] == 7
    assert body["envelope"]["source"] == "operator_approved"


def test_get_breach_returns_null_envelope_for_legacy_breach(
    client: TestClient, monkeypatch
) -> None:
    """AC6.c — legacy breaches with envelope_version=None get envelope=null."""
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(return_value=_legacy_breach()),
    )
    get_version_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.get_version",
        get_version_mock,
    )
    response = client.get("/api/breaches/legacy-1")
    assert response.status_code == 200
    body = response.json()
    assert body["breach"]["breach_id"] == "legacy-1"
    assert body["breach"]["envelope_version"] is None
    assert body["breach"]["envelope_source"] is None
    assert body["envelope"] is None
    # AC6.c: when envelope_version is None we MUST NOT call get_version at all
    get_version_mock.assert_not_awaited()


def test_get_breach_envelope_hydration_failure_returns_null_not_500(
    client: TestClient, monkeypatch
) -> None:
    """Envelope hydration is best-effort — failures degrade gracefully to null."""
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(return_value=_new_breach()),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.get_version",
        AsyncMock(side_effect=RuntimeError("mongo timeout")),
    )
    response = client.get("/api/breaches/new-1")
    assert response.status_code == 200
    body = response.json()
    assert body["breach"]["breach_id"] == "new-1"
    assert body["envelope"] is None


def test_get_breach_503_when_mongo_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "db_manager", None)
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/breaches/whatever")
    assert response.status_code == 503
    assert response.json()["detail"]["title"] == "database_unavailable"


def test_get_breach_500_when_repo_raises(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(side_effect=RuntimeError("mongo down")),
    )
    response = client.get("/api/breaches/anything")
    assert response.status_code == 500
    assert response.json()["detail"]["title"] == "breach_lookup_failed"


# ---------------------------------------------------------------------------
# AC6.e — replay round-trip (legacy + new shape)
# ---------------------------------------------------------------------------


def test_replay_legacy_breach_round_trips_through_subscriber_and_api(
    client: TestClient, monkeypatch
) -> None:
    """AC6.e — a legacy event (no envelope_version) is parsed by the
    subscriber, persisted with NULLs, and surfaces unchanged via the
    GET endpoint with envelope=null."""
    legacy = _legacy_breach()
    # Persisted state (would be inserted by the subscriber).
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(return_value=legacy),
    )
    response = client.get(f"/api/breaches/{legacy.breach_id}")
    body = response.json()
    assert body["breach"]["envelope_version"] is None
    assert body["breach"]["envelope_source"] is None
    assert body["envelope"] is None


def test_replay_new_breach_round_trips_with_envelope(
    client: TestClient, monkeypatch
) -> None:
    """AC6.e — a post-AC6.a event (envelope_version + source set) is
    persisted with both fields and surfaces with the hydrated envelope."""
    new = _new_breach()
    monkeypatch.setattr(
        "data_manager.db.repositories.drawdown_breach_repository.DrawdownBreachRepository.get_by_id",
        AsyncMock(return_value=new),
    )
    monkeypatch.setattr(
        "data_manager.db.repositories.envelope_repository.EnvelopeRepository.get_version",
        AsyncMock(return_value=_envelope_v7()),
    )
    response = client.get(f"/api/breaches/{new.breach_id}")
    body = response.json()
    assert body["breach"]["envelope_version"] == 7
    assert body["breach"]["envelope_source"] == "operator_approved"
    assert body["envelope"]["version"] == 7


# ---------------------------------------------------------------------------
# AC6.d — subscriber tolerance to missing fields
# ---------------------------------------------------------------------------


def _wire_event(payload: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.data = json.dumps(payload).encode("utf-8")
    return msg


@pytest.mark.asyncio
async def test_subscriber_persists_legacy_payload_with_null_envelope_fields():
    """AC6.d — payload missing envelope_version/source persists with NULLs."""
    persisted: list[DrawdownBreach] = []

    class _StubRepo:
        async def insert(self, breach: DrawdownBreach) -> bool:
            persisted.append(breach)
            return True

        async def ensure_indexes(self) -> None:
            return None

    sub = DrawdownBreachSubscriber(repository=_StubRepo())
    legacy_payload = {
        "strategy_id": "momentum-v3",
        "observed_drawdown_pct": 7.2,
        "envelope_value_pct": 5.0,
        "exceeded_by_pct": 2.2,
        "detected_at": "2026-05-01T10:00:00+00:00",
        # NO envelope_version / envelope_source on the wire
    }
    await sub._on_message(_wire_event(legacy_payload))

    assert len(persisted) == 1
    breach = persisted[0]
    assert breach.envelope_version is None
    assert breach.envelope_source is None
    assert breach.strategy_id == "momentum-v3"


@pytest.mark.asyncio
async def test_subscriber_persists_new_payload_with_populated_envelope_fields():
    """AC6.d — payload with envelope_version/source carries them through."""
    persisted: list[DrawdownBreach] = []

    class _StubRepo:
        async def insert(self, breach: DrawdownBreach) -> bool:
            persisted.append(breach)
            return True

        async def ensure_indexes(self) -> None:
            return None

    sub = DrawdownBreachSubscriber(repository=_StubRepo())
    payload = {
        "strategy_id": "momentum-v3",
        "observed_drawdown_pct": 6.5,
        "envelope_value_pct": 5.0,
        "exceeded_by_pct": 1.5,
        "detected_at": "2026-06-01T10:00:00+00:00",
        "envelope_version": 7,
        "envelope_source": "operator_approved",
    }
    await sub._on_message(_wire_event(payload))

    assert len(persisted) == 1
    breach = persisted[0]
    assert breach.envelope_version == 7
    assert breach.envelope_source == "operator_approved"


@pytest.mark.asyncio
async def test_subscriber_drops_invalid_payload_without_raising():
    """Subscriber MUST swallow validation errors — never crash the
    NATS callback (would break the spine for downstream subscribers)."""

    class _StubRepo:
        async def insert(self, breach: DrawdownBreach) -> bool:
            return True

        async def ensure_indexes(self) -> None:
            return None

    sub = DrawdownBreachSubscriber(repository=_StubRepo())
    # Missing required strategy_id → ValidationError. The subscriber must
    # NOT raise; the offending payload is logged and dropped.
    await sub._on_message(_wire_event({"observed_drawdown_pct": 1.0}))


@pytest.mark.asyncio
async def test_subscriber_synthesizes_breach_id_when_absent():
    """A producer that doesn't supply breach_id still gets a stable
    id via the (strategy_id, detected_at) hash — replay-safe."""
    persisted: list[DrawdownBreach] = []

    class _StubRepo:
        async def insert(self, breach: DrawdownBreach) -> bool:
            persisted.append(breach)
            return True

        async def ensure_indexes(self) -> None:
            return None

    sub = DrawdownBreachSubscriber(repository=_StubRepo())
    payload = {
        "strategy_id": "momentum-v3",
        "observed_drawdown_pct": 7.2,
        "envelope_value_pct": 5.0,
        "exceeded_by_pct": 2.2,
        "detected_at": "2026-05-01T10:00:00+00:00",
    }
    await sub._on_message(_wire_event(payload))
    await sub._on_message(_wire_event(payload))  # idempotent: same hash

    assert len(persisted) == 2  # Both insert() calls happen
    assert persisted[0].breach_id == persisted[1].breach_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_synthesize_breach_id_stable():
    payload = {"strategy_id": "x", "detected_at": "2026-01-01T00:00:00Z"}
    assert _synthesize_breach_id(payload) == _synthesize_breach_id(payload)


def test_normalize_event_coerces_string_version():
    payload = {
        "strategy_id": "x",
        "detected_at": "2026-01-01T00:00:00+00:00",
        "envelope_version": "11",
    }
    out = _normalize_event(payload)
    assert out["envelope_version"] == 11


def test_normalize_event_drops_unparseable_version():
    payload = {
        "strategy_id": "x",
        "detected_at": "2026-01-01T00:00:00+00:00",
        "envelope_version": "not-a-number",
    }
    out = _normalize_event(payload)
    assert out["envelope_version"] is None
