"""petrosa_k8s#1527: request metrics are labelled by route template, never by raw path values."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from data_manager.api.middleware.metrics import UNMATCHED_ENDPOINT, MetricsMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.post("/api/v1/leases/{name}/acquire")
    async def acquire(name: str) -> dict:
        return {"name": name}

    @app.get("/api/v1/risk/inputs")
    async def risk_inputs() -> dict:
        return {}

    return app


def _count(endpoint: str, method: str, status: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "data_manager_requests_total",
            {"method": method, "endpoint": endpoint, "status_code": status},
        )
        or 0.0
    )


def test_lease_names_share_one_template_label():
    client = TestClient(_app())
    template = "/api/v1/leases/{name}/acquire"
    before = _count(template, "POST", "200")
    for suffix in ("fa2bf3d866cf", "f37e464a9c1f", "f26a74dd5f8e"):
        assert (
            client.post(
                f"/api/v1/leases/signal_spread_ETHUSDT_{suffix}/acquire"
            ).status_code
            == 200
        )
    assert _count(template, "POST", "200") - before == 3
    raw = "/api/v1/leases/signal_spread_ETHUSDT_fa2bf3d866cf"
    assert _count(raw, "POST", "200") == 0


def test_static_routes_keep_their_label():
    """Alert rules filter endpoint=~"/api/v1/risk/inputs": that value must not change."""
    client = TestClient(_app())
    before = _count("/api/v1/risk/inputs", "GET", "200")
    assert client.get("/api/v1/risk/inputs").status_code == 200
    assert _count("/api/v1/risk/inputs", "GET", "200") - before == 1


def test_unmatched_paths_share_one_label():
    client = TestClient(_app())
    before = _count(UNMATCHED_ENDPOINT, "GET", "404")
    for path in ("/nope/a1", "/nope/b2", "/api/v1/unknown/x9"):
        assert client.get(path).status_code == 404
    assert _count(UNMATCHED_ENDPOINT, "GET", "404") - before == 3
    assert _count("/nope/a1", "GET", "404") == 0


def test_pre_routing_label_collapses_lease_names():
    """The Mongo source label is taken before routing: lease names must not reach it either."""
    from types import SimpleNamespace

    request = SimpleNamespace(
        url=SimpleNamespace(
            path="/api/v1/leases/signal_spread_ETHUSDT_fa2bf3d866cf/renew"
        )
    )
    assert MetricsMiddleware._extract_endpoint(None, request) == "/api/v1/leases/{name}"
