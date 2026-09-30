from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from data_manager.api import app as api_app
from data_manager.api.routes import config_rate_limits


class FakeLimiter:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.records = []
        self.__class__.instances.append(self)

    async def check_rate_limit(self, **kwargs):
        self.check = kwargs
        return {"allowed": True, "reason": "within_quota"}

    async def record_change(self, **kwargs):
        self.records.append(kwargs)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    FakeLimiter.instances.clear()
    monkeypatch.setattr(config_rate_limits, "ConfigRateLimiter", FakeLimiter)
    monkeypatch.setattr(
        api_app,
        "db_manager",
        SimpleNamespace(mongodb_adapter=SimpleNamespace(db=object())),
    )


@pytest.mark.asyncio
async def test_check_uses_request_service_and_returns_library_result():
    result = await config_rate_limits.check_rate_limit(
        config_rate_limits.RateLimitCheckRequest(
            service="trade-engine",
            changed_by="agent",
            endpoint="/config",
        )
    )

    assert result == {"allowed": True, "reason": "within_quota"}
    assert FakeLimiter.instances[0].kwargs["service_name"] == "trade-engine"


@pytest.mark.asyncio
async def test_record_returns_created_response_and_passes_fields():
    result = await config_rate_limits.record_change(
        config_rate_limits.RateLimitRecordRequest(
            service="data-manager",
            changed_by="operator",
            endpoint="/config/update",
            success=False,
        )
    )

    assert result == {"recorded": True}
    assert FakeLimiter.instances[0].records == [
        {"changed_by": "operator", "endpoint": "/config/update", "success": False}
    ]


def test_route_is_registered_before_generic_config_routes():
    check_route = next(
        route
        for route in config_rate_limits.router.routes
        if route.path == "/api/v1/config/rate-limits/check"
    )
    assert check_route.endpoint is config_rate_limits.check_rate_limit
    assert api_app.create_app()


@pytest.mark.asyncio
async def test_missing_database_returns_503():
    api_app.db_manager = None
    with pytest.raises(HTTPException) as exc_info:
        await config_rate_limits.check_rate_limit(
            config_rate_limits.RateLimitCheckRequest(
                service="data-manager", changed_by="agent", endpoint="/config"
            )
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_bookkeeping_paths_bypass_generic_throttle(monkeypatch):
    calls = []

    async def generic_middleware(request, call_next):
        calls.append(request.url.path)
        return await call_next(request)

    monkeypatch.setattr(api_app, "config_rate_limit_middleware", generic_middleware)

    async def call_next(request):
        return request.url.path

    for path in (
        "/api/v1/config/rate-limits/check",
        "/api/v1/config/rate-limits/record",
    ):
        request = SimpleNamespace(url=SimpleNamespace(path=path))
        assert await api_app._config_rate_limit_middleware(request, call_next) == path

    assert calls == []


@pytest.mark.asyncio
async def test_other_mutations_remain_throttled(monkeypatch):
    calls = []

    async def generic_middleware(request, call_next):
        calls.append(request.url.path)
        return await call_next(request)

    monkeypatch.setattr(api_app, "config_rate_limit_middleware", generic_middleware)

    async def call_next(request):
        return "allowed"

    request = SimpleNamespace(url=SimpleNamespace(path="/api/v1/config/application"))
    assert await api_app._config_rate_limit_middleware(request, call_next) == "allowed"
    assert calls == ["/api/v1/config/application"]
