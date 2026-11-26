"""petrosa_k8s#1527: lease names never reach the request-metric endpoint label; other labels are unchanged."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from data_manager.api.middleware.metrics import MetricsMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)
    leases = APIRouter(prefix="/api/v1/leases")
    risk = APIRouter()

    @leases.post("/{name}/acquire")
    async def acquire(name: str) -> dict:
        return {"name": name}

    @risk.get("/inputs")
    async def risk_inputs() -> dict:
        return {}

    app.include_router(leases)
    app.include_router(risk, prefix="/api/v1/risk")
    return app


def _count(endpoint: str, method: str, status: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "data_manager_requests_total",
            {"method": method, "endpoint": endpoint, "status_code": status},
        )
        or 0.0
    )


def test_lease_names_share_one_label():
    client = TestClient(_app())
    before = _count("/api/v1/leases/{name}", "POST", "200")
    for suffix in ("fa2bf3d866cf", "f37e464a9c1f", "f26a74dd5f8e"):
        response = client.post(f"/api/v1/leases/signal_spread_ETHUSDT_{suffix}/acquire")
        assert response.status_code == 200
    assert _count("/api/v1/leases/{name}", "POST", "200") - before == 3
    assert (
        _count("/api/v1/leases/signal_spread_ETHUSDT_fa2bf3d866cf", "POST", "200") == 0
    )


def test_prefixed_router_labels_are_unchanged():
    """Alert rules filter endpoint=~"/api/v1/risk/inputs": a router mounted with a prefix keeps it."""
    client = TestClient(_app())
    before = _count("/api/v1/risk/inputs", "GET", "200")
    assert client.get("/api/v1/risk/inputs").status_code == 200
    assert _count("/api/v1/risk/inputs", "GET", "200") - before == 1


def test_lease_collapse_applies_to_every_lease_path():
    from types import SimpleNamespace

    for path in (
        "/api/v1/leases/x_1/renew",
        "/api/v1/leases/x_2",
        "/api/v1/leases/tradeengine-leader/release",
    ):
        request = SimpleNamespace(url=SimpleNamespace(path=path))
        assert (
            MetricsMiddleware._extract_endpoint(None, request)
            == "/api/v1/leases/{name}"
        )
    other = SimpleNamespace(url=SimpleNamespace(path="/api/v1/mongodb/positions"))
    assert (
        MetricsMiddleware._extract_endpoint(None, other) == "/api/v1/mongodb/positions"
    )
