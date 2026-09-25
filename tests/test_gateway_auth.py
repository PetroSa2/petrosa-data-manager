"""Gateway authentication and CORS contract tests."""

from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.gateway_auth import _settings


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
