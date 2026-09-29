"""Gateway authentication and CORS contract tests."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import data_manager.api.app as api_module
import data_manager.api.gateway_auth as gateway_auth
from data_manager.api.gateway_auth import _settings


def _request(path: str, route_template: str | None = None) -> Request:
    scope = {
        "type": "http",
        "path": path,
        "headers": [(b"x-petrosa-service", b"unverified")],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
        "client": ("testclient", 123),
        "root_path": "",
    }
    if route_template is not None:
        scope["route"] = SimpleNamespace(path=route_template)
    return Request(scope)


@pytest.fixture(autouse=True)
def clear_gateway_auth_state():
    gateway_auth._settings.cache_clear()
    gateway_auth._AUDIT_LOGGED.clear()
    yield
    gateway_auth._settings.cache_clear()
    gateway_auth._AUDIT_LOGGED.clear()


def test_enforce_requires_and_accepts_service_token(monkeypatch):
    monkeypatch.setenv("DM_AUTH_MODE", "enforce")
    monkeypatch.setenv("DM_SERVICE_TOKENS", '{"test-service":"secret"}')
    _settings.cache_clear()
    client = TestClient(api_module.create_app())

    assert client.get("/").status_code == 401
    assert (
        client.get(
            "/",
            headers={
                "X-Petrosa-Service": "test-service",
                "Authorization": "Bearer wrong",
            },
        ).status_code
        == 401
    )
    response = client.get(
        "/",
        headers={
            "X-Petrosa-Service": "test-service",
            "Authorization": "Bearer secret",
        },
    )
    assert response.status_code == 200
    assert client.get("/health/liveness").status_code == 200


def test_cors_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DM_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("DM_AUTH_MODE", "off")
    _settings.cache_clear()
    response = TestClient(api_module.create_app()).get(
        "/", headers={"Origin": "http://x"}
    )

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_unverified_requests_dedupe_by_route_template(monkeypatch, caplog):
    monkeypatch.setenv("DM_AUTH_MODE", "audit")
    with caplog.at_level("WARNING", logger="data_manager.api.gateway_auth"):
        for index in range(100):
            await gateway_auth.require_service(
                _request(f"/positions/{index}", "/positions/{position_id}")
            )

    assert (
        sum("gateway_auth_unverified" in record.message for record in caplog.records)
        == 1
    )


@pytest.mark.asyncio
async def test_unverified_requests_log_different_route_templates_separately(
    monkeypatch, caplog
):
    monkeypatch.setenv("DM_AUTH_MODE", "audit")
    with caplog.at_level("WARNING", logger="data_manager.api.gateway_auth"):
        await gateway_auth.require_service(_request("/positions/1", "/positions/{id}"))
        await gateway_auth.require_service(_request("/orders/1", "/orders/{id}"))

    assert (
        sum("gateway_auth_unverified" in record.message for record in caplog.records)
        == 2
    )


@pytest.mark.asyncio
async def test_unmatched_route_audit_cache_is_bounded(monkeypatch):
    monkeypatch.setenv("DM_AUTH_MODE", "audit")
    for index in range(gateway_auth._AUDIT_LOGGED_MAX + 100):
        await gateway_auth.require_service(_request(f"/unmatched/{index}"))

    assert len(gateway_auth._AUDIT_LOGGED) == gateway_auth._AUDIT_LOGGED_MAX
