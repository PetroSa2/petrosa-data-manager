from datetime import date

import pytest
from fastapi import HTTPException

from data_manager.api.routes import ledger


def daily_payload(**overrides):
    payload = {
        "day": "2026-09-30",
        "source_run_id": "run-1",
        "is_final": False,
        "row_count": 1,
        "rows": [
            {
                "asset": "USDT",
                "income_by_type": {
                    "TRANSFER": "-1.00000000",
                    "COMMISSION_REBATE": "0.50000000",
                },
            }
        ],
        "wallet_balance": "10.00000000",
        "balance_as_of_ms": 100,
        "income_after_day_end": {},
    }
    payload.update(overrides)
    return payload


def test_amounts_are_strict_strings_and_symbol_defaults():
    model = ledger.DailyLedger.model_validate(daily_payload())
    assert model.rows[0].symbol == ""
    with pytest.raises(ValueError):
        ledger.DailyLedger.model_validate(daily_payload(wallet_balance=10))


def test_daily_payload_rejects_inconsistent_row_count():
    with pytest.raises(ValueError, match="row_count") as error:
        ledger.DailyLedger.model_validate(daily_payload(row_count=2))
    assert "row_count" in str(error.value)


@pytest.mark.asyncio
async def test_daily_route_rejects_path_mismatch():
    body = ledger.DailyLedger.model_validate(daily_payload())
    with pytest.raises(HTTPException) as error:
        await ledger.put_exchange_daily(date(2026, 10, 1), body)
    assert error.value.status_code == 422


def test_economic_hash_ignores_balance_and_source_run():
    first = daily_payload()
    second = daily_payload(balance_as_of_ms=999, source_run_id="run-2")
    assert ledger.LedgerRepository.economic_hash(
        first
    ) == ledger.LedgerRepository.economic_hash(second)
