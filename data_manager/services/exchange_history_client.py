"""Read the account's exchange fills and orders from tradeengine (dm#844).

Data-manager holds no Binance account credentials. Tradeengine does, and exposes the
account's own history read-only (PetroSa2/petrosa-tradeengine#833):
``GET /api/v1/exchange/fills`` (userTrades) and ``/api/v1/exchange/orders``
(allOrders). Each call covers at most one 7-day span and returns pages of at most
1000 rows keyed by id; this client walks both.

Authentication: ``X-Petrosa-Service: data-manager`` plus the bearer token of the
tradeengine<->data-manager service identity, the ``tradeengine`` entry of
``DM_SERVICE_TOKENS`` (the same secret tradeengine mounts as ``DM_SERVICE_TOKEN``).
The token is read from the environment and is never logged or returned.

Any non-200 answer raises :class:`ExchangeHistoryError` with the status and the
route's error code, so a caller reports the gap instead of a partial history.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://petrosa-tradeengine-service"
CALLER_SERVICE = "data-manager"
# The route's limits (tradeengine #833): one exchange query span and one page.
SPAN_MS = 7 * 24 * 60 * 60 * 1000
PAGE_ROWS = 1000


class ExchangeHistoryError(RuntimeError):
    """A history read that did not return 200."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"exchange history read failed: HTTP {status} {error}")
        self.status = status
        self.error = error


def _pair_token() -> str:
    try:
        tokens = json.loads(os.environ.get("DM_SERVICE_TOKENS", "{}"))
    except json.JSONDecodeError:
        return ""
    token = tokens.get("tradeengine") if isinstance(tokens, dict) else None
    return token if isinstance(token, str) else ""


class ExchangeHistoryClient:
    """Walks tradeengine's read-only fills/orders routes over a time range."""

    def __init__(
        self,
        base_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("TRADEENGINE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._http = http
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        token = _pair_token()
        if not token:
            raise ExchangeHistoryError(0, "service_token_unconfigured")
        return {"X-Petrosa-Service": CALLER_SERVICE, "Authorization": f"Bearer {token}"}

    async def fills(
        self, symbol: str, start_ms: int, end_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Every account fill on ``symbol`` in ``[start_ms, end_ms]``, by trade id."""
        return await self._walk(
            "/api/v1/exchange/fills", "id", symbol, start_ms, end_ms
        )

    async def orders(
        self, symbol: str, start_ms: int, end_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Every account order on ``symbol`` created in ``[start_ms, end_ms]``."""
        return await self._walk(
            "/api/v1/exchange/orders", "orderId", symbol, start_ms, end_ms
        )

    async def _walk(
        self,
        path: str,
        id_key: str,
        symbol: str,
        start_ms: int,
        end_ms: int | None,
    ) -> list[dict[str, Any]]:
        end = int(time.time() * 1000) if end_ms is None else end_ms
        headers = self._headers()
        rows: dict[Any, dict[str, Any]] = {}
        owns_client = self._http is None
        http = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            span_start = start_ms
            while span_start <= end:
                span_end = min(span_start + SPAN_MS - 1, end)
                after_id: int | None = None
                while True:
                    params: dict[str, Any] = {
                        "symbol": symbol,
                        "start_ms": span_start,
                        "end_ms": span_end,
                        "limit": PAGE_ROWS,
                    }
                    if after_id is not None:
                        params["after_id"] = after_id
                    response = await http.get(
                        f"{self._base_url}{path}", params=params, headers=headers
                    )
                    if response.status_code != 200:
                        raise ExchangeHistoryError(
                            response.status_code, _error_code(response)
                        )
                    body = response.json()
                    for row in body.get("rows", []):
                        rows[row.get(id_key)] = row
                    after_id = body.get("next_after_id")
                    if after_id is None:
                        break
                span_start = span_end + 1
        finally:
            if owns_client:
                await http.aclose()
        return sorted(rows.values(), key=lambda r: int(r.get(id_key) or 0))


def _error_code(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return "unreadable_error"
    if isinstance(detail, dict):
        return str(detail.get("error") or "unknown_error")
    return str(detail or "unknown_error")[:80]
