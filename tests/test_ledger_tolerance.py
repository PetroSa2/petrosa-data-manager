from decimal import Decimal

from data_manager.services.ledger_tolerance import (
    ExchangeInfoCache,
    calculate_tolerance,
    fallback_limits,
    median_absolute_deviation,
)


def test_mad_and_tolerance_include_thirty_day_variance_term():
    values = [Decimal(str(index)) for index in range(30)]
    assert median_absolute_deviation(values) == Decimal("7.5")
    result = calculate_tolerance(
        [{"qty": "2", "commission_asset_precision": "0.01", "tick_size": "0.1"}],
        values,
    )
    assert result["rounding_bound"] == "0.105"
    assert result["mad"] == "7.5"
    assert result["mad_term"] == "33.35850"
    assert result["source"] == "source: exchange-info+variance"
    assert result["sample_days"] == "30"


def test_fallback_limits_scale_with_equity_and_missing_precision():
    limits = fallback_limits(Decimal("2000"))
    assert limits == {"daily": "20.00", "cumulative": "10", "per_item": "5"}
    result = calculate_tolerance([{"qty": "2"}], [])
    assert result["amount"] == "1"
    assert result["source"] == "source: fallback"


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "quoteAssetPrecision": 2,
                    "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}],
                }
            ]
        }


class FakeClient:
    def __init__(self):
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return FakeResponse()


def test_exchange_info_cache_refreshes_once_per_day():
    client = FakeClient()
    cache = ExchangeInfoCache(client)
    assert cache.get("BTCUSDT") == {
        "tick_size": "0.10",
        "commission_asset_precision": "0.01",
    }
    assert cache.get("UNKNOWN") is None
    assert client.calls == 1
