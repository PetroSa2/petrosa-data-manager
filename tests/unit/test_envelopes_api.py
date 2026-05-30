"""Tests for /api/envelopes routes (#187, P4.6-AC1 / FR62).

Covers every AC of #187:

* AC1.a — ``GET /api/envelopes/pending``: lists pending changes; empty
  list when none exist; 503 when DB unavailable.
* AC1.b — ``POST /api/envelopes/{change_id}/accept``: happy path writes
  a new Envelope, flips status, emits audit; 404 on missing change_id;
  409 when already-resolved.
* AC1.c — ``POST /api/envelopes/{change_id}/accept-with-modification``:
  merges overrides onto proposed value; rest mirrors AC1.b.
* AC1.d — ``POST /api/envelopes/{change_id}/reject``: requires rationale;
  flips status, emits audit; no new Envelope written.
* AC1.e — Every resolution endpoint records ``operator_id`` and
  ``signed_action_id``.
* AC1.f — Validated implicitly via OpenAPI generation (handler
  signatures + Pydantic models); explicitly via 422 on missing
  ``operator_id`` body field.
* AC1.g — Every action emits to ``envelope_authorship_audit``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes.envelopes import (
    ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION,
    ENVELOPES_CHANGED_NATS_SUBJECT,
    set_envelopes_changed_publisher,
)
from data_manager.db.repositories.envelope_repository import ENVELOPES_COLLECTION
from data_manager.db.repositories.pending_envelope_change_repository import (
    PENDING_ENVELOPE_CHANGES_COLLECTION,
)

# ─── In-memory Mongo fake — mirrors tests/unit/test_leverage_bounds_routes.py,
#     extended with update_one + projection support for our resolve() flow. ──


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]], projection: dict | None = None):
        self._docs = docs
        self._sort_field: str | None = None
        self._sort_direction = 1
        self._limit: int | None = None
        self._projection = projection
        self._iter: Any = None

    def sort(self, field, direction):
        # Accept either ("field", 1) positional pair OR sort("field", 1)
        # depending on which the repo uses.
        self._sort_field = field
        self._sort_direction = direction
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __aiter__(self):
        docs = [dict(d) for d in self._docs]
        if self._sort_field is not None:
            docs.sort(
                key=lambda d: d.get(self._sort_field, 0),
                reverse=self._sort_direction < 0,
            )
        if self._limit is not None:
            docs = docs[: self._limit]
        if self._projection is not None:
            keep = {k for k, v in self._projection.items() if v}
            docs = [{k: v for k, v in d.items() if k in keep} for d in docs]
        self._iter = iter(docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as e:
            raise StopAsyncIteration from e


class _UpdateResult:
    def __init__(self, matched: int) -> None:
        self.matched_count = matched


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def find_one(self, query: dict, sort=None, projection=None):
        candidates = [d for d in self.docs if _matches(d, query)]
        if sort:
            field, direction = sort[0]
            candidates.sort(key=lambda d: d.get(field, 0), reverse=direction < 0)
        if not candidates:
            return None
        doc = dict(candidates[0])
        if projection:
            keep = {k for k, v in projection.items() if v}
            doc = {k: v for k, v in doc.items() if k in keep or k == "_id"}
            # _id default excluded only when explicitly set to 0/False
            if projection.get("_id") in (0, False):
                doc.pop("_id", None)
        return doc

    def find(self, query: dict, projection=None):
        matched = [d for d in self.docs if _matches(d, query)]
        return _FakeCursor(matched, projection=projection)

    async def insert_one(self, doc: dict):
        d = dict(doc)
        if "_id" not in d:
            d["_id"] = f"_autogen-{len(self.docs)}"
        for existing in self.docs:
            if existing.get("_id") == d["_id"]:
                from pymongo.errors import DuplicateKeyError

                raise DuplicateKeyError(f"duplicate key _id={d['_id']!r}")
        self.docs.append(d)

    async def update_one(self, query: dict, update: dict):
        for d in self.docs:
            if _matches(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                return _UpdateResult(matched=1)
        return _UpdateResult(matched=0)

    async def create_index(self, *args, **kwargs):  # noqa: ARG002
        return None


def _matches(doc: dict, query: dict) -> bool:
    return all(doc.get(k) == v for k, v in query.items())


class _FakeDb:
    def __init__(self) -> None:
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())


class _FakeMongoAdapter:
    def __init__(self) -> None:
        self.db = _FakeDb()


class _FakeDbManager:
    def __init__(self) -> None:
        self.mongodb_adapter = _FakeMongoAdapter()
        self.mysql_adapter = None


# ─── Fixtures ────────────────────────────────────────────────────────────────


class _RecordingPublisher:
    """Captures (subject, payload-dict) tuples for assertion in tests."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode())))


class _BrokenPublisher:
    """Always raises — used to prove publish failures never break the operator request."""

    async def publish(self, subject: str, payload: bytes) -> None:
        raise RuntimeError("nats down")


@pytest.fixture
def wired_app():
    """Wire the in-memory Mongo + recording NATS publisher into the app module."""
    original = api_module.db_manager
    api_module.db_manager = _FakeDbManager()
    publisher = _RecordingPublisher()
    set_envelopes_changed_publisher(publisher)
    try:
        app = create_app()
        client = TestClient(app)
        # Stash handles for in-test seeding/inspection.
        client.mongo = api_module.db_manager.mongodb_adapter  # type: ignore[attr-defined]
        client.publisher = publisher  # type: ignore[attr-defined]
        yield client
    finally:
        api_module.db_manager = original
        set_envelopes_changed_publisher(None)


def _seed_pending(
    mongo: _FakeMongoAdapter,
    *,
    change_id: str = "chg-1",
    key: str = "strategy:btc_momentum_v3",
    proposed: dict[str, Any] | None = None,
    current_version: int | None = 3,
    current_value: dict[str, Any] | None = None,
    char_revision: str = "char-rev-42",
    status: str = "pending",
    resolution: dict | None = None,
    created_at: str = "2026-05-29T10:00:00+00:00",
) -> None:
    """Insert a PendingEnvelopeChange document directly into the fake Mongo."""
    doc = {
        "_id": change_id,
        "change_id": change_id,
        "strategy_or_portfolio_key": key,
        "proposed_envelope_value": proposed or {"max_drawdown_pct": 8.0},
        "current_envelope_version": current_version,
        "current_envelope_value": current_value or {"max_drawdown_pct": 5.0},
        "diverging_pct_per_strategy": {key: 60.0},
        "originating_characterization_revision": char_revision,
        "created_at": created_at,
        "status": status,
        "resolution": resolution,
    }
    mongo.db[PENDING_ENVELOPE_CHANGES_COLLECTION].docs.append(doc)


def _seed_existing_envelope(
    mongo: _FakeMongoAdapter,
    *,
    key: str,
    version: int,
    value: dict[str, Any],
) -> None:
    """Seed a prior Envelope so next insert lands at version+1."""
    doc = {
        "_id": f"{key}:v{version}",
        "envelope_id": f"{key}:v{version}",
        "version": version,
        "strategy_or_portfolio_key": key,
        "value": value,
        "source": "characterization",
        "originating_characterization_revision": "char-rev-prior",
        "operator_id": None,
        "created_at": "2026-05-28T00:00:00+00:00",
        "signed_action_id": "sa-prior",
    }
    mongo.db[ENVELOPES_COLLECTION].docs.append(doc)


# ─── AC1.a — GET /api/envelopes/pending ──────────────────────────────────────


def test_get_pending_returns_empty_when_no_changes(wired_app: TestClient) -> None:
    r = wired_app.get("/api/envelopes/pending")
    assert r.status_code == 200
    body = r.json()
    assert body == {"pending": [], "count": 0}


def test_get_pending_returns_seeded_change(wired_app: TestClient) -> None:
    _seed_pending(wired_app.mongo, change_id="chg-1")
    r = wired_app.get("/api/envelopes/pending")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["pending"][0]["change_id"] == "chg-1"
    assert body["pending"][0]["status"] == "pending"


def test_get_pending_excludes_already_resolved(wired_app: TestClient) -> None:
    """Already-accepted/rejected changes don't surface on /pending."""
    _seed_pending(
        wired_app.mongo,
        change_id="chg-old",
        status="accepted",
        resolution={
            "operator_id": "op1",
            "signed_action_id": "sa1",
            "resolved_at": "2026-05-28T00:00:00+00:00",
            "modification_overrides": None,
            "rejection_reason": None,
        },
    )
    _seed_pending(wired_app.mongo, change_id="chg-new")
    r = wired_app.get("/api/envelopes/pending")
    body = r.json()
    assert body["count"] == 1
    assert body["pending"][0]["change_id"] == "chg-new"


def test_get_pending_503_when_db_unavailable() -> None:
    original = api_module.db_manager
    api_module.db_manager = None
    try:
        app = create_app()
        client = TestClient(app)
        r = client.get("/api/envelopes/pending")
        assert r.status_code == 503
        assert r.json()["detail"]["title"] == "MongoDB unavailable"
    finally:
        api_module.db_manager = original


# ─── AC1.b — POST /accept ────────────────────────────────────────────────────


def test_accept_happy_path_writes_envelope_flips_status_and_audits(
    wired_app: TestClient,
) -> None:
    _seed_pending(
        wired_app.mongo,
        change_id="chg-1",
        key="strategy:btc",
        proposed={"max_drawdown_pct": 8.0},
        current_version=2,
    )
    _seed_existing_envelope(
        wired_app.mongo,
        key="strategy:btc",
        version=2,
        value={"max_drawdown_pct": 5.0},
    )

    r = wired_app.post(
        "/api/envelopes/chg-1/accept",
        json={"operator_id": "alice", "signed_action_id": "sa-alpha"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # New envelope written at version 3 (next after seeded 2).
    assert body["envelope"]["version"] == 3
    assert body["envelope"]["source"] == "operator_approved"
    assert body["envelope"]["operator_id"] == "alice"
    assert body["envelope"]["signed_action_id"] == "sa-alpha"
    assert body["envelope"]["value"] == {"max_drawdown_pct": 8.0}

    # Pending change flipped to accepted, carries the resolution.
    assert body["change"]["status"] == "accepted"
    assert body["change"]["resolution"]["operator_id"] == "alice"
    assert body["change"]["resolution"]["modification_overrides"] is None

    # Audit collection received exactly one row of the right kind (AC1.g).
    audit_docs = wired_app.mongo.db[ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION].docs
    assert len(audit_docs) == 1
    audit = audit_docs[0]
    assert audit["kind"] == "envelope_change_accepted"
    assert audit["operator_id"] == "alice"
    assert audit["signed_action_id"] == "sa-alpha"
    assert audit["before_envelope_version"] == 2
    assert audit["after_envelope_version"] == 3
    assert audit["accepted_envelope_value"] == {"max_drawdown_pct": 8.0}


def test_accept_404_when_change_missing(wired_app: TestClient) -> None:
    r = wired_app.post(
        "/api/envelopes/does-not-exist/accept",
        json={"operator_id": "alice", "signed_action_id": "sa-alpha"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["title"] == "pending envelope change not found"


def test_accept_409_when_already_resolved(wired_app: TestClient) -> None:
    _seed_pending(
        wired_app.mongo,
        change_id="chg-1",
        status="rejected",
        resolution={
            "operator_id": "op-prev",
            "signed_action_id": "sa-prev",
            "resolved_at": "2026-05-28T00:00:00+00:00",
            "modification_overrides": None,
            "rejection_reason": "stale characterization",
        },
    )
    r = wired_app.post(
        "/api/envelopes/chg-1/accept",
        json={"operator_id": "alice", "signed_action_id": "sa-alpha"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["title"] == "pending envelope change already resolved"


def test_accept_422_when_operator_id_missing(wired_app: TestClient) -> None:
    """AC1.e — operator_id and signed_action_id are non-optional."""
    _seed_pending(wired_app.mongo, change_id="chg-1")
    r = wired_app.post(
        "/api/envelopes/chg-1/accept",
        json={"signed_action_id": "sa-only"},
    )
    assert r.status_code == 422


# ─── AC1.c — POST /accept-with-modification ──────────────────────────────────


def test_accept_with_modification_merges_overrides(
    wired_app: TestClient,
) -> None:
    _seed_pending(
        wired_app.mongo,
        change_id="chg-mod",
        key="strategy:eth",
        proposed={"max_drawdown_pct": 8.0, "stop_loss_pct": 2.0},
    )

    r = wired_app.post(
        "/api/envelopes/chg-mod/accept-with-modification",
        json={
            "operator_id": "bob",
            "signed_action_id": "sa-mod",
            "modification_overrides": {"max_drawdown_pct": 7.5},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Overrides win on top-level keys; other keys passthrough.
    assert body["envelope"]["value"] == {"max_drawdown_pct": 7.5, "stop_loss_pct": 2.0}
    assert body["change"]["resolution"]["modification_overrides"] == {
        "max_drawdown_pct": 7.5
    }

    audit_docs = wired_app.mongo.db[ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION].docs
    assert len(audit_docs) == 1
    assert audit_docs[0]["kind"] == "envelope_change_accepted_with_modification"
    assert audit_docs[0]["modification_overrides"] == {"max_drawdown_pct": 7.5}


def test_accept_with_modification_requires_overrides_body(
    wired_app: TestClient,
) -> None:
    _seed_pending(wired_app.mongo, change_id="chg-mod")
    r = wired_app.post(
        "/api/envelopes/chg-mod/accept-with-modification",
        json={"operator_id": "bob", "signed_action_id": "sa-mod"},
    )
    assert r.status_code == 422


# ─── AC1.d — POST /reject ────────────────────────────────────────────────────


def test_reject_writes_no_envelope_emits_audit_with_rationale(
    wired_app: TestClient,
) -> None:
    _seed_pending(wired_app.mongo, change_id="chg-rej", key="strategy:doge")

    r = wired_app.post(
        "/api/envelopes/chg-rej/reject",
        json={
            "operator_id": "carol",
            "signed_action_id": "sa-rej",
            "rejection_reason": "characterization sample size too small",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change"]["status"] == "rejected"
    assert (
        body["change"]["resolution"]["rejection_reason"]
        == "characterization sample size too small"
    )
    # No Envelope was written.
    assert wired_app.mongo.db[ENVELOPES_COLLECTION].docs == []
    # But audit row was.
    audit_docs = wired_app.mongo.db[ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION].docs
    assert len(audit_docs) == 1
    assert audit_docs[0]["kind"] == "envelope_change_rejected"
    assert audit_docs[0]["after_envelope_version"] is None


def test_reject_requires_rationale_body(wired_app: TestClient) -> None:
    """AC1.d — rejection_reason is non-optional."""
    _seed_pending(wired_app.mongo, change_id="chg-rej")
    r = wired_app.post(
        "/api/envelopes/chg-rej/reject",
        json={"operator_id": "carol", "signed_action_id": "sa-rej"},
    )
    assert r.status_code == 422


def test_reject_404_when_change_missing(wired_app: TestClient) -> None:
    r = wired_app.post(
        "/api/envelopes/missing/reject",
        json={
            "operator_id": "carol",
            "signed_action_id": "sa-rej",
            "rejection_reason": "n/a",
        },
    )
    assert r.status_code == 404


# ─── AC3 / #193 — envelopes.changed NATS publisher ───────────────────────────


def test_accept_publishes_envelopes_changed(wired_app: TestClient) -> None:
    """AC3: successful accept emits envelopes.changed with the AC payload shape."""
    _seed_pending(
        wired_app.mongo,
        change_id="chg-pub-1",
        key="strategy:btc_pub",
        proposed={"max_drawdown_pct": 7.5},
        char_revision="char-rev-pub-1",
    )

    r = wired_app.post(
        "/api/envelopes/chg-pub-1/accept",
        json={"operator_id": "alice", "signed_action_id": "sa-pub-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nats_published"] is True

    assert len(wired_app.publisher.published) == 1
    subject, payload = wired_app.publisher.published[0]
    assert subject == ENVELOPES_CHANGED_NATS_SUBJECT
    assert subject == "envelopes.changed"
    assert payload["strategy_or_portfolio_key"] == "strategy:btc_pub"
    assert isinstance(payload["new_version"], int)
    assert payload["source"] == "operator_approved"
    assert payload["signed_action_id"] == "sa-pub-1"
    assert payload["originating_characterization_revision"] == "char-rev-pub-1"
    assert payload["accepted_at"] is not None


def test_accept_with_modification_publishes_envelopes_changed(
    wired_app: TestClient,
) -> None:
    _seed_pending(
        wired_app.mongo,
        change_id="chg-pub-2",
        key="strategy:eth_pub",
        proposed={"max_drawdown_pct": 6.0, "vol_target": 0.1},
        char_revision="char-rev-pub-2",
    )

    r = wired_app.post(
        "/api/envelopes/chg-pub-2/accept-with-modification",
        json={
            "operator_id": "bob",
            "signed_action_id": "sa-pub-2",
            "modification_overrides": {"vol_target": 0.15},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["nats_published"] is True

    assert len(wired_app.publisher.published) == 1
    subject, payload = wired_app.publisher.published[0]
    assert subject == "envelopes.changed"
    assert payload["strategy_or_portfolio_key"] == "strategy:eth_pub"
    assert payload["source"] == "operator_approved"
    assert payload["signed_action_id"] == "sa-pub-2"


def test_reject_publishes_envelopes_changed_with_status_rejected(
    wired_app: TestClient,
) -> None:
    _seed_pending(wired_app.mongo, change_id="chg-pub-3", key="strategy:doge_pub")

    r = wired_app.post(
        "/api/envelopes/chg-pub-3/reject",
        json={
            "operator_id": "carol",
            "signed_action_id": "sa-pub-3",
            "rejection_reason": "characterization sample too small",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["nats_published"] is True

    assert len(wired_app.publisher.published) == 1
    subject, payload = wired_app.publisher.published[0]
    assert subject == "envelopes.changed"
    assert payload["strategy_or_portfolio_key"] == "strategy:doge_pub"
    assert payload["status"] == "rejected"
    assert payload["change_id"] == "chg-pub-3"
    assert payload["operator_id"] == "carol"
    assert payload["signed_action_id"] == "sa-pub-3"
    # Accept-only fields must be absent on reject payload (top-level filter)
    assert "new_version" not in payload
    assert "source" not in payload


def test_broken_publisher_does_not_fail_accept(wired_app: TestClient) -> None:
    """AC: publish failures are logged but never break the operator request."""
    set_envelopes_changed_publisher(_BrokenPublisher())
    _seed_pending(wired_app.mongo, change_id="chg-broken", key="strategy:broken")

    r = wired_app.post(
        "/api/envelopes/chg-broken/accept",
        json={"operator_id": "alice", "signed_action_id": "sa-broken"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["nats_published"] is False
    # Envelope + audit still landed even though publish raised.
    assert len(wired_app.mongo.db[ENVELOPES_COLLECTION].docs) == 1
    assert len(wired_app.mongo.db[ENVELOPE_AUTHORSHIP_AUDIT_COLLECTION].docs) == 1


def test_publisher_unwired_returns_nats_published_false(wired_app: TestClient) -> None:
    """No publisher wired → route still succeeds, returns nats_published=False."""
    set_envelopes_changed_publisher(None)
    _seed_pending(wired_app.mongo, change_id="chg-no-pub", key="strategy:noop")

    r = wired_app.post(
        "/api/envelopes/chg-no-pub/accept",
        json={"operator_id": "alice", "signed_action_id": "sa-no-pub"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["nats_published"] is False
    # Storage + audit still happen.
    assert len(wired_app.mongo.db[ENVELOPES_COLLECTION].docs) == 1
