"""Tests for the P3.2 characterization model + repository + API (#599)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.db.repositories.characterization_repository import (
    CHARACTERIZATIONS_COLLECTION,
    CharacterizationRepository,
)
from data_manager.models.characterization import (
    Characterization,
    compute_inputs_hash,
    verify_characterization,
)

T0 = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)


def _make_artifact(
    *,
    strategy_id: str = "s1",
    strategy_version: str = "v1",
    seed: int = 42,
    metrics: dict[str, float] | None = None,
    drawdown_envelope: list[float] | None = None,
    param_sensitivities: dict[str, object] | None = None,
    inputs_hash: str | None = None,
) -> Characterization:
    return Characterization(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        data_window_from=T0,
        data_window_to=T1,
        seed=seed,
        metrics=metrics or {"sharpe": 1.5, "win_rate": 0.55, "mean_return": 0.0012},
        drawdown_envelope=drawdown_envelope or [-0.01, -0.05, -0.1, -0.2],
        param_sensitivities=param_sensitivities or {"window": [10, 20, 30]},
        inputs_hash=inputs_hash
        or compute_inputs_hash(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            data_window_from=T0,
            data_window_to=T1,
            seed=seed,
        ),
    )


# ----------------------------------------------------------------------
# Model + helpers.
# ----------------------------------------------------------------------


def test_artifact_round_trips_through_model_dump():
    artifact = _make_artifact()
    dumped = artifact.model_dump()
    rebuilt = Characterization(**dumped)
    assert rebuilt.model_dump() == dumped


def test_validate_required_metrics_raises_on_missing_key():
    artifact = _make_artifact(
        metrics={"sharpe": 1.0, "win_rate": 0.5}
    )  # no mean_return
    with pytest.raises(ValueError, match="mean_return") as exc_info:
        artifact.validate_required_metrics()
    assert "mean_return" in str(exc_info.value)


def test_validate_required_metrics_accepts_extra_keys():
    """Extra metric keys are tolerated and round-tripped."""
    artifact = _make_artifact(
        metrics={
            "sharpe": 1.0,
            "win_rate": 0.5,
            "mean_return": 0.001,
            "calmar": 0.4,  # extra metric, no error
        }
    )
    artifact.validate_required_metrics()  # should not raise
    assert artifact.metrics["calmar"] == 0.4


def test_compute_inputs_hash_is_stable_across_dict_orderings():
    h1 = compute_inputs_hash(
        strategy_id="s1",
        strategy_version="v1",
        data_window_from=T0,
        data_window_to=T1,
        seed=42,
        params={"a": 1, "b": 2},
    )
    h2 = compute_inputs_hash(
        strategy_id="s1",
        strategy_version="v1",
        data_window_from=T0,
        data_window_to=T1,
        seed=42,
        params={"b": 2, "a": 1},
    )
    assert h1 == h2


def test_compute_inputs_hash_changes_with_seed():
    h1 = compute_inputs_hash(
        strategy_id="s1",
        strategy_version="v1",
        data_window_from=T0,
        data_window_to=T1,
        seed=42,
    )
    h2 = compute_inputs_hash(
        strategy_id="s1",
        strategy_version="v1",
        data_window_from=T0,
        data_window_to=T1,
        seed=43,
    )
    assert h1 != h2


def test_compute_inputs_hash_treats_naive_as_utc():
    """Naive datetimes are assumed UTC so hashes stay stable."""
    naive = T0.replace(tzinfo=None)
    h_aware = compute_inputs_hash(
        strategy_id="s",
        strategy_version="v",
        data_window_from=T0,
        data_window_to=T1,
        seed=1,
    )
    h_naive = compute_inputs_hash(
        strategy_id="s",
        strategy_version="v",
        data_window_from=naive,
        data_window_to=T1,
        seed=1,
    )
    assert h_aware == h_naive


def test_verify_characterization_passes_when_recompute_matches():
    artifact = _make_artifact()

    def _recompute(**kwargs):
        # Return an artifact with identical metrics + envelope + sensitivities.
        return _make_artifact()

    assert verify_characterization(artifact, _recompute) is True


def test_verify_characterization_fails_when_metric_drifts():
    artifact = _make_artifact()

    def _recompute(**kwargs):
        # Tiny drift in sharpe — must not pass.
        return _make_artifact(
            metrics={
                "sharpe": 1.5000001,
                "win_rate": 0.55,
                "mean_return": 0.0012,
            }
        )

    assert verify_characterization(artifact, _recompute) is False


def test_verify_characterization_fails_when_inputs_hash_differs():
    artifact = _make_artifact()

    def _recompute(**kwargs):
        return _make_artifact(inputs_hash="0" * 64)

    assert verify_characterization(artifact, _recompute) is False


# ----------------------------------------------------------------------
# Repository (mocked mongo).
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_upsert_uses_deterministic_id():
    mongo = MagicMock()
    coll = MagicMock()
    coll.replace_one = AsyncMock()
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=mongo)

    artifact = _make_artifact(strategy_id="alpha", strategy_version="v3")
    assert await repo.upsert(artifact) is True

    coll.replace_one.assert_awaited_once()
    args, kwargs = coll.replace_one.call_args
    filt, doc = args[0], args[1]
    assert filt == {"_id": "alpha::v3"}
    assert doc["_id"] == "alpha::v3"
    assert doc["strategy_id"] == "alpha"
    assert kwargs.get("upsert") is True


@pytest.mark.asyncio
async def test_repo_upsert_raises_on_missing_required_metric():
    mongo = MagicMock()
    coll = MagicMock()
    coll.replace_one = AsyncMock()
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=mongo)

    artifact = _make_artifact(metrics={"sharpe": 1.0, "win_rate": 0.5})
    with pytest.raises(ValueError):
        await repo.upsert(artifact)


@pytest.mark.asyncio
async def test_repo_upsert_returns_false_when_mongo_missing():
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=None)
    assert await repo.upsert(_make_artifact()) is False


@pytest.mark.asyncio
async def test_repo_get_version_round_trips():
    mongo = MagicMock()
    coll = MagicMock()
    artifact = _make_artifact()
    stored = artifact.model_dump()
    stored["_id"] = "s1::v1"
    coll.find_one = AsyncMock(return_value=stored)
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=mongo)

    result = await repo.get_version("s1", "v1")
    assert result is not None
    assert result.strategy_id == "s1"
    assert result.strategy_version == "v1"


@pytest.mark.asyncio
async def test_repo_get_version_returns_none_when_missing():
    mongo = MagicMock()
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=None)
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=mongo)
    assert await repo.get_version("missing", "v9") is None


@pytest.mark.asyncio
async def test_repo_get_latest_uses_sort_and_limit():
    mongo = MagicMock()
    coll = MagicMock()
    artifact = _make_artifact()
    stored = artifact.model_dump()
    stored["_id"] = "s1::v1"
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[stored])
    coll.find = MagicMock(return_value=cursor)
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}
    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=mongo)

    result = await repo.get_latest("s1")
    assert result is not None
    cursor.sort.assert_called_once_with("created_at", -1)
    cursor.limit.assert_called_once_with(1)


# ----------------------------------------------------------------------
# API endpoint (FastAPI TestClient).
# ----------------------------------------------------------------------


@pytest.fixture()
def client_with_artifact():
    """FastAPI TestClient with a stub db_manager wired to deterministic data."""
    app = create_app()
    artifact = _make_artifact()

    mongo = MagicMock()
    coll = MagicMock()
    stored = artifact.model_dump()
    stored["_id"] = f"{artifact.strategy_id}::{artifact.strategy_version}"

    # find_one returns the stored doc when (strategy_id, version) matches,
    # None otherwise.
    async def _find_one(filt):
        if (
            filt.get("strategy_id") == artifact.strategy_id
            and filt.get("strategy_version") == artifact.strategy_version
        ):
            return stored
        return None

    coll.find_one = AsyncMock(side_effect=_find_one)

    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[stored])
    coll.find = MagicMock(return_value=cursor)

    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}

    db_manager_stub = MagicMock()
    db_manager_stub.mongodb_adapter = mongo
    db_manager_stub.mysql_adapter = None

    api_module.db_manager = db_manager_stub
    try:
        yield TestClient(app), artifact
    finally:
        api_module.db_manager = None


def test_endpoint_returns_artifact_by_version(client_with_artifact):
    client, artifact = client_with_artifact
    r = client.get(
        "/api/v1/characterizations",
        params={
            "strategy_id": artifact.strategy_id,
            "version": artifact.strategy_version,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == artifact.strategy_id
    assert body["strategy_version"] == artifact.strategy_version
    assert body["metrics"]["sharpe"] == artifact.metrics["sharpe"]


def test_endpoint_returns_latest_when_no_version(client_with_artifact):
    client, artifact = client_with_artifact
    r = client.get(
        "/api/v1/characterizations",
        params={"strategy_id": artifact.strategy_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == artifact.strategy_id


def test_endpoint_404_when_version_missing(client_with_artifact):
    client, artifact = client_with_artifact
    r = client.get(
        "/api/v1/characterizations",
        params={"strategy_id": artifact.strategy_id, "version": "nope"},
    )
    assert r.status_code == 404
    assert "no characterization" in r.json()["detail"]


def test_endpoint_503_when_db_unavailable():
    app = create_app()
    api_module.db_manager = None
    client = TestClient(app)
    r = client.get("/api/v1/characterizations", params={"strategy_id": "any"})
    assert r.status_code == 503


# ----------------------------------------------------------------------
# FR53 / P3.4 (#179) — strategy_revision_id field + filter + verify drift.
# ----------------------------------------------------------------------


from data_manager.models.characterization import StrategyRevisionRef  # noqa: E402

_SREV_HASH_A = "a" * 64
_SREV_HASH_B = "b" * 64
_SREV_ID_A = "srev_" + "a" * 12 + "_" + "a" * 12  # mirrors producer scheme
_SREV_ID_B = "srev_" + "b" * 12 + "_" + "b" * 12


def _make_artifact_with_revision(
    *, revision_id: str = _SREV_ID_A, module_hash: str = _SREV_HASH_A
) -> Characterization:
    art = _make_artifact()
    art.strategy_revision_id = revision_id
    art.strategy_revision = StrategyRevisionRef(
        revision_id=revision_id,
        module_hash=module_hash,
        parameter_hash=_SREV_HASH_B,
    )
    return art


def test_artifact_round_trip_preserves_strategy_revision_id():
    """AC1 — model round-trips strategy_revision_id + nested ref."""
    artifact = _make_artifact_with_revision()
    dumped = artifact.model_dump()
    rebuilt = Characterization(**dumped)
    assert rebuilt.strategy_revision_id == _SREV_ID_A
    assert rebuilt.strategy_revision is not None
    assert rebuilt.strategy_revision.revision_id == _SREV_ID_A
    assert rebuilt.strategy_revision.module_hash == _SREV_HASH_A


def test_artifact_strategy_revision_defaults_none_for_pre_p34_documents():
    """AC1 — legacy documents (no revision binding) still parse cleanly."""
    artifact = _make_artifact()
    assert artifact.strategy_revision_id is None
    assert artifact.strategy_revision is None
    dumped = artifact.model_dump()
    rebuilt = Characterization(**dumped)
    assert rebuilt.strategy_revision_id is None


def test_verify_characterization_detects_revision_id_drift():
    """AC4 — verify_characterization must detect revision-id mismatch."""
    artifact_a = _make_artifact_with_revision(revision_id=_SREV_ID_A)

    def _recompute_with_b(**_kw) -> Characterization:
        # Same metrics / drawdown / inputs_hash — only the revision_id differs.
        out = _make_artifact_with_revision(revision_id=_SREV_ID_B)
        out.inputs_hash = artifact_a.inputs_hash
        out.metrics = dict(artifact_a.metrics)
        out.drawdown_envelope = list(artifact_a.drawdown_envelope)
        out.param_sensitivities = dict(artifact_a.param_sensitivities)
        return out

    assert verify_characterization(artifact_a, _recompute_with_b) is False

    def _recompute_with_a(**_kw) -> Characterization:
        return _make_artifact_with_revision(revision_id=_SREV_ID_A)

    assert verify_characterization(artifact_a, _recompute_with_a) is True


@pytest.fixture()
def client_with_revision_artifact():
    """TestClient where the stored artifact carries a revision id; the mock
    cursor filters faithfully on `strategy_revision_id`.
    """
    app = create_app()
    artifact = _make_artifact_with_revision(revision_id=_SREV_ID_A)

    mongo = MagicMock()
    coll = MagicMock()
    stored = artifact.model_dump()
    stored["_id"] = f"{artifact.strategy_id}::{artifact.strategy_version}"

    async def _find_one(filt):
        if (
            filt.get("strategy_id") == artifact.strategy_id
            and filt.get("strategy_version") == artifact.strategy_version
        ):
            return stored
        return None

    coll.find_one = AsyncMock(side_effect=_find_one)

    def _find(filt):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        if (
            "strategy_revision_id" in filt
            and filt["strategy_revision_id"] != artifact.strategy_revision_id
        ):
            cursor.to_list = AsyncMock(return_value=[])
        else:
            cursor.to_list = AsyncMock(return_value=[stored])
        return cursor

    coll.find = MagicMock(side_effect=_find)
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}

    db_manager_stub = MagicMock()
    db_manager_stub.mongodb_adapter = mongo
    db_manager_stub.mysql_adapter = None
    api_module.db_manager = db_manager_stub
    try:
        yield TestClient(app), artifact
    finally:
        api_module.db_manager = None


def test_endpoint_returns_artifact_filtered_by_strategy_revision_id(
    client_with_revision_artifact,
):
    """AC3 — strategy_revision_id filter returns the matching artifact."""
    client, artifact = client_with_revision_artifact
    r = client.get(
        "/api/v1/characterizations",
        params={
            "strategy_id": artifact.strategy_id,
            "strategy_revision_id": artifact.strategy_revision_id,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_revision_id"] == _SREV_ID_A
    assert body["strategy_revision"]["revision_id"] == _SREV_ID_A


def test_endpoint_404_on_stale_strategy_revision_id(client_with_revision_artifact):
    """AC3 — mismatched strategy_revision_id is 404 (consumer refusal signal)."""
    client, artifact = client_with_revision_artifact
    r = client.get(
        "/api/v1/characterizations",
        params={
            "strategy_id": artifact.strategy_id,
            "strategy_revision_id": _SREV_ID_B,  # stale revision
        },
    )
    assert r.status_code == 404
    assert _SREV_ID_B in r.json()["detail"]


@pytest.mark.asyncio
async def test_repo_get_by_strategy_revision_matches_and_misses():
    """AC2 — repo lookup returns the matching artifact and None for stale id."""
    artifact = _make_artifact_with_revision(revision_id=_SREV_ID_A)
    stored = artifact.model_dump()
    stored["_id"] = f"{artifact.strategy_id}::{artifact.strategy_version}"

    coll = MagicMock()

    def _find(filt):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        if filt.get("strategy_revision_id") == artifact.strategy_revision_id:
            cursor.to_list = AsyncMock(return_value=[stored])
        else:
            cursor.to_list = AsyncMock(return_value=[])
        return cursor

    coll.find = MagicMock(side_effect=_find)

    mongo = MagicMock()
    mongo.db = {CHARACTERIZATIONS_COLLECTION: coll}

    repo = CharacterizationRepository(mysql_adapter=None, mongodb_adapter=mongo)

    hit = await repo.get_by_strategy_revision(artifact.strategy_id, _SREV_ID_A)
    assert hit is not None
    assert hit.strategy_revision_id == _SREV_ID_A

    miss = await repo.get_by_strategy_revision(artifact.strategy_id, _SREV_ID_B)
    assert miss is None
