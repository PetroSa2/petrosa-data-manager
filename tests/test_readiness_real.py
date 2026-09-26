"""/health/readiness reflects MongoDB (data-manager#378, G8).

It was hardcoded to ``ready=True``. Now: ready iff the database manager exists
and a Mongo ping answers within READINESS_MONGO_TIMEOUT_SECONDS; the result is
cached so probes never pile up; MySQL is reported but never gates readiness.
"""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import constants
import data_manager.api.app as api_module
from data_manager.api.routes import health


@pytest.fixture(autouse=True)
def reset_cache():
    health._readiness_cache = None
    yield
    health._readiness_cache = None
    api_module.db_manager = None


@pytest.fixture
def mongo():
    adapter = Mock()
    adapter.db = Mock()
    adapter.db.command = AsyncMock(return_value={"ok": 1.0})
    return adapter


@pytest.fixture
def mysql():
    adapter = Mock()
    adapter.is_connected = Mock(return_value=True)
    return adapter


@pytest.fixture
def client(mock_db_manager, mongo, mysql):
    mock_db_manager.mongodb_adapter = mongo
    mock_db_manager.mysql_adapter = mysql
    api_module.db_manager = mock_db_manager
    return TestClient(api_module.create_app())


def _readiness(client):
    response = client.get("/health/readiness")
    return response.status_code, response.json()


def test_successful_ping_is_ready(client, mongo):
    status, body = _readiness(client)

    assert status == 200
    assert body["ready"] is True
    assert body["components"]["mongodb"] == "healthy"
    assert body["components"]["mysql"] == "healthy"
    mongo.db.command.assert_awaited_once_with("ping")


def test_failing_ping_is_503_with_the_same_body_shape(client, mongo):
    mongo.db.command.side_effect = ConnectionError("no primary")

    status, body = _readiness(client)

    assert status == 503
    assert body["ready"] is False
    assert body["components"]["mongodb"] == "unavailable"
    assert set(body) == {"ready", "components", "timestamp"}


def test_ping_timeout_is_503_within_the_budget(client, mongo, monkeypatch):
    monkeypatch.setattr(constants, "READINESS_MONGO_TIMEOUT_SECONDS", 0.05)

    async def hang(_command):
        await asyncio.sleep(5)

    mongo.db.command.side_effect = hang

    started = time.monotonic()
    status, body = _readiness(client)

    assert status == 503
    assert body["ready"] is False
    assert time.monotonic() - started < 2


def test_no_database_manager_is_503(client):
    api_module.db_manager = None

    status, body = _readiness(client)

    assert status == 503
    assert body["ready"] is False
    assert body["components"]["mongodb"] == "unavailable"
    assert body["components"]["mysql"] == "unavailable"


@pytest.mark.parametrize("missing", ["adapter", "db"])
def test_mongo_not_connected_is_503(client, mongo, missing):
    if missing == "adapter":
        api_module.db_manager.mongodb_adapter = None
    else:
        mongo.db = None

    status, body = _readiness(client)

    assert status == 503
    assert body["ready"] is False


def test_mysql_unavailable_with_mongo_up_is_still_ready(client):
    api_module.db_manager.mysql_adapter = None

    status, body = _readiness(client)

    assert status == 200
    assert body["ready"] is True
    assert body["components"]["mysql"] == "unavailable"


def test_mysql_disconnected_is_reported_but_does_not_gate(client, mysql):
    mysql.is_connected.return_value = False

    status, body = _readiness(client)

    assert status == 200
    assert body["components"]["mysql"] == "unavailable"


def test_result_is_cached_so_probes_do_not_pile_up(client, mongo):
    for _ in range(3):
        status, _body = _readiness(client)
        assert status == 200

    assert mongo.db.command.await_count == 1


def test_cache_expires_and_readiness_recovers(client, mongo):
    mongo.db.command.side_effect = ConnectionError("no primary")
    assert _readiness(client)[0] == 503

    mongo.db.command.side_effect = None
    # Still inside the cache window: the failure is served from cache.
    assert _readiness(client)[0] == 503

    evaluated_at, ready, components = health._readiness_cache
    health._readiness_cache = (
        evaluated_at - health._READINESS_CACHE_SECONDS - 1,
        ready,
        components,
    )
    assert _readiness(client)[0] == 200
    assert mongo.db.command.await_count == 2
