"""Tolerance calculations for exchange-ledger tie-outs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Any

import httpx

MAD_SCALE = Decimal("4.4478")
FALLBACK_DAILY = Decimal("1")
FALLBACK_CUMULATIVE = Decimal("10")
FALLBACK_ITEM = Decimal("5")
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def median_absolute_deviation(values: Iterable[Decimal]) -> Decimal:
    samples = list(values)
    if not samples:
        return Decimal("0")
    centre = median(samples)
    return median([abs(value - centre) for value in samples])


def calculate_tolerance(
    fills: Iterable[Mapping[str, Any]],
    trailing_unexplained: Iterable[Decimal],
    *,
    fallback: Decimal = FALLBACK_DAILY,
) -> dict[str, str | dict[str, str]]:
    """Return rounding, MAD, and source components for one symbol-day."""
    rounding_bound = Decimal("0")
    has_exchange_precision = False
    for fill in fills:
        quantity = abs(_decimal(fill.get("quantity", fill.get("qty", "0"))))
        commission_precision = fill.get("commission_asset_precision")
        tick_size = fill.get("tick_size")
        if commission_precision is None or tick_size is None:
            continue
        has_exchange_precision = True
        rounding_bound += (_decimal(commission_precision) / 2) + (
            _decimal(tick_size) * quantity / 2
        )

    trailing = list(trailing_unexplained)
    mad = (
        median_absolute_deviation([_decimal(value) for value in trailing])
        if len(trailing) >= 30
        else Decimal("0")
    )
    mad_term = MAD_SCALE * mad if len(trailing) >= 30 else Decimal("0")
    calculated = rounding_bound + mad_term
    source = "source: exchange-info+variance" if has_exchange_precision else "source: fallback"
    if not has_exchange_precision:
        calculated = max(calculated, _decimal(fallback))
    return {
        "amount": str(calculated),
        "source": source,
        "rounding_bound": str(rounding_bound),
        "mad": str(mad),
        "mad_term": str(mad_term),
        "sample_days": str(len(trailing)),
    }


def fallback_limits(equity: Decimal | None = None) -> dict[str, str]:
    """Return the documented limits used when exchange or history data is absent."""
    daily = max(FALLBACK_DAILY, abs(equity or Decimal("0")) * Decimal("0.01"))
    cumulative = max(
        FALLBACK_CUMULATIVE, abs(equity or Decimal("0")) * Decimal("0.001")
    )
    return {
        "daily": str(daily),
        "cumulative": str(cumulative),
        "per_item": str(FALLBACK_ITEM),
    }


class ExchangeInfoCache:
    """Daily in-process cache for Binance public symbol precision filters."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._day = None
        self._symbols: dict[str, dict[str, str]] = {}

    def _refresh(self) -> None:
        client = self._client or httpx.Client(timeout=10.0)
        close = self._client is None
        try:
            response = client.get(EXCHANGE_INFO_URL)
            response.raise_for_status()
            payload = response.json()
        finally:
            if close:
                client.close()
        symbols: dict[str, dict[str, str]] = {}
        for symbol in payload.get("symbols", []):
            filters = {item["filterType"]: item for item in symbol.get("filters", [])}
            price = filters.get("PRICE_FILTER", {})
            symbols[symbol["symbol"]] = {
                "tick_size": str(price.get("tickSize", "0")),
                "commission_asset_precision": str(
                    Decimal("1").scaleb(-int(symbol.get("quoteAssetPrecision", 8)))
                ),
            }
        self._symbols = symbols
        self._day = date.today()

    def get(self, symbol: str) -> dict[str, str] | None:
        if self._day != date.today():
            self._refresh()
        return self._symbols.get(symbol)
