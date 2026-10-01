import hashlib
import json
from datetime import date, datetime

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


class Result:
    def __init__(self, row=None, scalar=1):
        self.row = row
        self.scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self.row

    def scalar_one(self):
        return self.scalar

    def all(self):
        return self.row or []


class FakeLedgerRepository(ledger.LedgerRepository):
    def __init__(self, responses):
        super().__init__(None, None)
        self.responses = iter(responses)
        self.statements = []

    def _run(self, statement, params=None):
        self.statements.append(statement)
        return next(self.responses, Result())


def test_insert_day_is_idempotent_and_records_revision():
    payload = daily_payload()
    repo = FakeLedgerRepository([Result(None), Result(scalar=1), Result()])
    result = repo.insert_day(payload, datetime.now())
    assert result["created"] is True
    assert result["revision"] == 1
    assert len(repo.statements) == 4
    same = FakeLedgerRepository(
        [Result({"payload_hash": repo.economic_hash(payload), "revision": 1})]
    )
    assert same.insert_day(payload, datetime.now())["created"] is False


def test_insert_day_final_restatement_increments_metric():
    payload = daily_payload(is_final=True)
    previous = {"payload_hash": "different", "revision": 1, "is_final": True}
    repo = FakeLedgerRepository(
        [Result(previous), Result(scalar=2), Result(), Result(), Result()]
    )
    result = repo.insert_day(payload, datetime.now())
    assert result["restated"] is True
    assert any("ledger_exchange_metrics" in statement for statement in repo.statements)


def test_position_snapshot_deduplicates_and_writes_rows():
    payload = {
        "as_of_ms": 1000,
        "source_run_id": "run",
        "rows": [
            {
                "symbol": "BTCUSDT",
                "position_side": "LONG",
                "quantity": "1",
                "entry_price": "2",
                "mark_price": "3",
                "unrealized_pnl": "1",
            }
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            {"rows": payload["rows"]}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    repo = FakeLedgerRepository([Result(None), Result(), Result()])
    assert repo.put_positions(payload, datetime.now())["created"] is True
    same = FakeLedgerRepository([Result({"payload_hash": digest, "as_of_ms": 900})])
    assert same.put_positions(payload, datetime.now())["created"] is False


@pytest.mark.asyncio
async def test_routes_delegate_to_repository(monkeypatch):
    class Repo:
        def insert_day(self, payload, received_at):
            return {"created": True}

        def put_positions(self, payload, received_at):
            return {"created": True}

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())
    daily = ledger.DailyLedger.model_validate(daily_payload())
    assert (await ledger.put_exchange_daily(date(2026, 9, 30), daily))["created"]
    positions = ledger.PositionsSnapshot(as_of_ms=10, source_run_id="run")
    assert (await ledger.put_exchange_positions(10, positions))["created"]


def test_tieout_returns_daily_components_and_cumulative_variance():
    repo = FakeLedgerRepository(
        [
            Result(
                [
                    {
                        "day": date(2026, 9, 30),
                        "realized_pnl": "-40",
                        "commission": "-2.92",
                        "funding_fee": "-4",
                        "other_income_total": "0",
                        "is_final": True,
                    }
                ]
            ),
            Result([{"date": date(2026, 9, 30), "daily_pnl": "0"}]),
        ]
    )
    day = repo.tieout(date(2026, 9, 30), date(2026, 9, 30))["days"][0]
    assert day["realized_and_fees"]["exchange"] == "-42.92"
    assert day["realized_and_fees"]["variance"] == "42.92"
    assert day["funding"]["status"] == "unbooked_by_design"


def test_positions_tieout_maps_sides_and_reports_phantoms():
    repo = FakeLedgerRepository(
        [
            Result({"as_of_ms": 100}),
            Result([{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]),
            Result(
                [
                    {"symbol": "BTCUSDT", "position_side": "BUY", "quantity": "1"},
                    {"symbol": "ETHUSDT", "position_side": "SELL", "quantity": "1"},
                ]
            ),
        ]
    )
    result = repo.positions_tieout()
    assert result["ledger_open_rows"][0]["position_side"] == "LONG"
    assert len(result["phantom_rows"]) == 1
