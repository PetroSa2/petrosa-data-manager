"""Client for tradeengine's read-only fills/orders routes (dm#844)."""

from __future__ import annotations

import json

import httpx
import pytest

from data_manager.services.exchange_history_client import (
    PAGE_ROWS,
    SPAN_MS,
    ExchangeHistoryClient,
    ExchangeHistoryError,
)

PAIR = "test-pair-value"

pytestmark = pytest.mark.asyncio


class _Route:
    """Serves the #833 routes from in-memory rows, enforcing their limits and auth."""

    def __init__(self, fills: list[dict], page_rows: int = PAGE_ROWS) -> None:
        self.fills = fills
        self.page_rows = page_rows
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append({**params, "path": request.url.path})
        if request.headers.get("Authorization") != f"Bearer {PAIR}":
            return httpx.Response(401, json={"detail": {"error": "unauthorized"}})
        if request.headers.get("X-Petrosa-Service") != "data-manager":
            return httpx.Response(403, json={"detail": {"error": "caller_not_allowed"}})
        start, end = int(params["start_ms"]), int(params["end_ms"])
        assert end - start < SPAN_MS
        after = int(params.get("after_id", -1))
        rows = sorted(
            (
                f
                for f in self.fills
                if f["symbol"] == params["symbol"]
                and start <= f["time"] <= end
                and f["id"] > after
            ),
            key=lambda f: f["id"],
        )
        page = rows[: self.page_rows]
        more = len(rows) > self.page_rows
        return httpx.Response(
            200,
            json={
                "rows": page,
                "count": len(page),
                "next_after_id": page[-1]["id"] if more else None,
            },
        )


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setenv("DM_SERVICE_TOKENS", json.dumps({"tradeengine": PAIR}))


def _client(route) -> ExchangeHistoryClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    return ExchangeHistoryClient(base_url="http://te", http=http)


async def test_walks_spans_and_pages_and_sends_the_identity() -> None:
    day = 24 * 60 * 60 * 1000
    fills = [{"symbol": "BTCUSDT", "id": i, "time": i * day // 2} for i in range(1, 41)]
    fills.append({"symbol": "ETHUSDT", "id": 99, "time": day})
    route = _Route(fills, page_rows=3)

    rows = await _client(route).fills("BTCUSDT", 0, 20 * day)

    assert [r["id"] for r in rows] == list(range(1, 41))
    assert {c["path"] for c in route.calls} == {"/api/v1/exchange/fills"}
    spans = {(c["start_ms"], c["end_ms"]) for c in route.calls}
    assert len(spans) == 3  # 20 days -> three 7-day spans
    assert any("after_id" in c for c in route.calls)


async def test_orders_use_the_orders_route() -> None:
    route = _Route([])
    await _client(route).orders("BTCUSDT", 0, 1000)
    assert route.calls[0]["path"] == "/api/v1/exchange/orders"


async def test_non_200_raises_with_the_route_error() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502, json={"detail": {"error": "exchange_read_failed", "reason": "x"}}
        )

    with pytest.raises(ExchangeHistoryError) as err:
        await _client(failing).fills("BTCUSDT", 0, 1000)
    assert err.value.status == 502 and err.value.error == "exchange_read_failed"


async def test_wrong_token_is_a_401_error(monkeypatch) -> None:
    monkeypatch.setenv("DM_SERVICE_TOKENS", json.dumps({"tradeengine": "other"}))
    with pytest.raises(ExchangeHistoryError) as err:
        await _client(_Route([])).fills("BTCUSDT", 0, 1000)
    assert err.value.status == 401


async def test_missing_token_fails_before_any_call(monkeypatch) -> None:
    monkeypatch.delenv("DM_SERVICE_TOKENS", raising=False)
    route = _Route([])
    with pytest.raises(ExchangeHistoryError) as err:
        await _client(route).fills("BTCUSDT", 0, 1000)
    assert err.value.error == "service_token_unconfigured"
    assert route.calls == []


async def test_errors_never_carry_the_token(monkeypatch) -> None:
    monkeypatch.setenv("DM_SERVICE_TOKENS", json.dumps({"tradeengine": "other"}))
    with pytest.raises(ExchangeHistoryError) as err:
        await _client(_Route([])).fills("BTCUSDT", 0, 1000)
    assert "other" not in str(err.value) and PAIR not in str(err.value)
