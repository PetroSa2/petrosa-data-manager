import hashlib
import json
import time
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

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


def test_ledger_repository_uses_mongodb_for_live_tieout_overlay(monkeypatch):
    mysql = object()
    mongo = object()
    monkeypatch.setattr(
        ledger.api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=mysql, mongodb_adapter=mongo),
    )

    repository = ledger._repo()

    assert repository.mysql is mysql
    assert repository.mongodb is mongo


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


@pytest.mark.asyncio
async def test_daily_route_returns_explicit_error_when_database_write_fails(
    monkeypatch,
):
    class Repo:
        def insert_day(self, _payload, _received_at):
            raise RuntimeError("daily ledger write failed")

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())
    body = ledger.DailyLedger.model_validate(daily_payload())

    with pytest.raises(HTTPException) as error:
        await ledger.put_exchange_daily(date(2026, 9, 30), body)

    assert error.value.status_code == 503
    assert error.value.detail["message"] == "daily ledger write failed"


@pytest.mark.asyncio
async def test_positions_route_returns_explicit_error_when_database_write_fails(
    monkeypatch,
):
    class Repo:
        def put_positions(self, _payload, _received_at):
            raise RuntimeError("positions write failed")

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())
    body = ledger.PositionsSnapshot(as_of_ms=10, source_run_id="run")

    with pytest.raises(HTTPException) as error:
        await ledger.put_exchange_positions(10, body)

    assert error.value.status_code == 503
    assert error.value.detail["message"] == "positions write failed"


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


@pytest.mark.asyncio
async def test_tieout_routes_publish_metrics_and_validate_range(monkeypatch):
    class Repo:
        def tieout(self, first, last):
            return {"days": [{"unexplained": "1.25"}], "cumulative_variance": "1.25"}

        def positions_tieout(self):
            return {
                "phantom_rows": [],
                "ledger_open_rows": [],
                "exchange_positions": [],
            }

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())
    result = await ledger.get_tieout(date(2026, 9, 30), date(2026, 9, 30))
    assert result["cumulative_variance"] == "1.25"
    assert (await ledger.get_positions_tieout())["phantom_rows"] == []
    with pytest.raises(HTTPException) as error:
        await ledger.get_tieout(date(2026, 10, 2), date(2026, 10, 1))
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_positions_tieout_returns_explicit_error_when_ledger_read_fails(
    monkeypatch,
):
    class Repo:
        def positions_tieout(self):
            raise RuntimeError("ledger positions query failed")

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())

    with pytest.raises(HTTPException) as error:
        await ledger.get_positions_tieout()

    assert error.value.status_code == 503
    assert error.value.detail == {
        "error": "Ledger database unavailable",
        "exception": "RuntimeError",
        "message": "ledger positions query failed",
    }


@pytest.mark.asyncio
async def test_tieout_returns_explicit_error_when_database_read_fails(monkeypatch):
    class Repo:
        def tieout(self, _first, _last):
            raise RuntimeError("closed range read failed")

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())

    with pytest.raises(HTTPException) as error:
        await ledger.get_tieout(date(2026, 9, 30), date(2026, 9, 30))

    assert error.value.status_code == 503
    assert error.value.detail == {
        "error": "Ledger database unavailable",
        "exception": "RuntimeError",
        "message": "closed range read failed",
    }


@pytest.mark.asyncio
async def test_positions_tieout_returns_503_when_database_read_times_out(
    monkeypatch,
):
    class Repo:
        def positions_tieout(self):
            time.sleep(0.05)
            return {
                "phantom_rows": [],
                "ledger_open_rows": [],
                "exchange_positions": [],
            }

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())
    monkeypatch.setattr("constants.API_QUERY_TIMEOUT", 0.001)

    with pytest.raises(HTTPException) as error:
        await ledger.get_positions_tieout()

    assert error.value.status_code == 503
    assert error.value.detail["error"] == "Ledger database unavailable"
    assert error.value.detail["exception"] == "TimeoutError"
    assert error.value.detail["message"]


@pytest.mark.asyncio
async def test_positions_tieout_retries_after_failed_shared_read(monkeypatch):
    calls = 0

    class Repo:
        def positions_tieout(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary ledger failure")
            return {
                "phantom_rows": [],
                "ledger_open_rows": [],
                "exchange_positions": [],
            }

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())

    with pytest.raises(HTTPException) as error:
        await ledger.get_positions_tieout()
    assert error.value.status_code == 503

    result = await ledger.get_positions_tieout()

    assert result["phantom_rows"] == []
    assert calls == 2


@pytest.mark.asyncio
async def test_positions_tieout_route_returns_matching_exchange_ledger_pairs(
    monkeypatch,
):
    expected = {
        "incomplete": False,
        "source_status": {"exchange": "available", "ledger": "available"},
        "quantity_tieout": [
            {
                "symbol": "BTCUSDT",
                "position_side": "LONG",
                "ledger_open_quantity": "1",
                "exchange_quantity": "1",
                "difference": "0",
                "status": "tied",
                "explanation": "Ledger and exchange quantities match within the 0.01 tolerance.",
            }
        ],
        "phantom_rows": [],
    }

    class Repo:
        def positions_tieout(self):
            return expected

    monkeypatch.setattr(ledger, "_repo", lambda: Repo())

    result = await ledger.get_positions_tieout()

    assert set(result["quantity_tieout"][0]) >= {
        "symbol",
        "position_side",
        "ledger_open_quantity",
        "exchange_quantity",
        "difference",
        "status",
        "explanation",
    }
    assert result["quantity_tieout"][0]["status"] == "tied"
    assert result["quantity_tieout"][0]["ledger_open_quantity"] == "1"


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


def test_tieout_rounding_only_difference_is_inside_tolerance():
    from data_manager.services.ledger_tolerance import calculate_tolerance

    tolerance = calculate_tolerance(
        [{"quantity": "1", "commission_asset_precision": "0.01", "tick_size": "0.10"}],
        [],
    )
    assert Decimal(tolerance["amount"]) == Decimal("0.055")
    assert Decimal("0.05") < Decimal(tolerance["amount"])


def test_tieout_real_break_exceeds_tolerance():
    from data_manager.services.ledger_tolerance import calculate_tolerance

    tolerance = calculate_tolerance([], [])
    assert Decimal("1.01") > Decimal(tolerance["amount"])


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
    assert result["incomplete"] is False
    assert result["source_status"] == {
        "exchange": "available",
        "ledger": "available",
    }
    assert result["ledger_open_rows"][0]["position_side"] == "LONG"
    assert len(result["phantom_rows"]) == 1


def test_positions_tieout_lists_every_exchange_position():
    repo = FakeLedgerRepository(
        [
            Result({"as_of_ms": 100}),
            Result(
                [
                    {"symbol": "BTCUSDT", "position_side": "BUY", "quantity": "1"},
                    {"symbol": "ETHUSDT", "position_side": "SELL", "quantity": "2"},
                ]
            ),
            Result([]),
        ]
    )

    result = repo.positions_tieout()

    assert result["exchange_positions"] == [
        {
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "exchange_positions": 1,
            "quantity": "1",
        },
        {
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "exchange_positions": 1,
            "quantity": "2",
        },
    ]


def test_positions_tieout_excludes_live_reconciler_closes():
    repo = FakeLedgerRepository(
        [
            Result({"as_of_ms": 100}),
            Result([{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]),
            Result(
                [
                    {
                        "entry_order_id": "reconciled-entry",
                        "symbol": "BTCUSDT",
                        "position_side": "BUY",
                        "quantity": "1",
                    },
                    {
                        "entry_order_id": "still-open",
                        "symbol": "BTCUSDT",
                        "position_side": "BUY",
                        "quantity": "0.25",
                    },
                ]
            ),
        ]
    )
    repo._external_close_ids_for_tieout = lambda: {"reconciled-entry"}

    result = repo.positions_tieout()

    btc = next(row for row in result["quantity_tieout"] if row["symbol"] == "BTCUSDT")
    assert btc["ledger_open_rows"] == 1
    assert btc["ledger_open_quantity"] == "0.25"
    assert btc["status"] == "exchange_exceeds_ledger"


def test_tieout_live_close_overlay_requires_mongo_when_mysql_is_available():
    repo = FakeLedgerRepository([])
    repo.mysql = object()

    with pytest.raises(RuntimeError, match="MongoDB is not available") as error:
        repo._external_close_ids_for_tieout()
    assert "MongoDB" in str(error.value)


def test_tieout_live_close_overlay_reads_external_close_ids(monkeypatch):
    repo = FakeLedgerRepository([])
    repo.mongodb = object()

    async def read_ids():
        return {"external-entry"}

    monkeypatch.setattr(repo, "live_closed_entry_order_ids", read_ids)

    assert repo._external_close_ids_for_tieout() == {"external-entry"}


def test_positions_tieout_compares_open_quantity_with_the_exchange_quantity():
    # ETH LONG: 17 open ledger rows of 0.01 against one exchange position of 0.05
    eth_rows = [{"symbol": "ETHUSDT", "position_side": "BUY", "quantity": "0.01"}] * 17
    repo = FakeLedgerRepository(
        [
            Result({"as_of_ms": 100}),
            Result(
                [
                    {"symbol": "ETHUSDT", "position_side": "LONG", "quantity": "0.05"},
                    {"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "0.002"},
                    {"symbol": "XRPUSDT", "position_side": "SHORT", "quantity": "-100"},
                ]
            ),
            Result(
                eth_rows
                + [
                    {"symbol": "BTCUSDT", "position_side": "BUY", "quantity": "0.001"},
                    {"symbol": "BTCUSDT", "position_side": "BUY", "quantity": "0.001"},
                    {"symbol": "XRPUSDT", "position_side": "SELL", "quantity": "100"},
                ]
            ),
        ]
    )
    result = repo.positions_tieout()
    by_key = {(r["symbol"], r["position_side"]): r for r in result["quantity_tieout"]}
    eth = by_key[("ETHUSDT", "LONG")]
    assert eth["ledger_open_rows"] == 17
    assert eth["ledger_open_quantity"] == "0.17"
    assert eth["exchange_quantity"] == "0.05"
    assert eth["difference"] == "0.12"
    assert eth["status"] == "ledger_exceeds_exchange"
    assert eth["explanation"] == "Ledger open quantity exceeds the exchange quantity."
    # two rows that add up to the exchange quantity are tied, however many rows there are
    assert by_key[("BTCUSDT", "LONG")]["status"] == "tied"
    assert (
        by_key[("BTCUSDT", "LONG")]["explanation"]
        == "Ledger and exchange quantities match within the 0.01 tolerance."
    )
    # a short exchange quantity is signed negative on the exchange; the comparison uses its size
    assert by_key[("XRPUSDT", "SHORT")]["status"] == "tied"
    assert (
        by_key[("XRPUSDT", "SHORT")]["explanation"]
        == "Ledger and exchange quantities match within the 0.01 tolerance."
    )
    assert [(r["symbol"], r["status"]) for r in result["quantity_mismatches"]] == [
        ("ETHUSDT", "ledger_exceeds_exchange")
    ]


def test_positions_tieout_reports_missing_ledger_quantity_and_a_flat_exchange_side():
    repo = FakeLedgerRepository(
        [
            Result({"as_of_ms": 100}),
            Result([{"symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}]),
            Result([{"symbol": "ETHUSDT", "position_side": "BUY", "quantity": "2"}]),
        ]
    )
    by_key = {
        (r["symbol"], r["position_side"]): r
        for r in repo.positions_tieout()["quantity_tieout"]
    }
    assert by_key[("BTCUSDT", "LONG")]["status"] == "exchange_exceeds_ledger"
    assert by_key[("ETHUSDT", "LONG")]["status"] == "ledger_exceeds_exchange"
    assert by_key[("ETHUSDT", "LONG")]["exchange_quantity"] == "0"


def test_positions_tieout_marks_missing_exchange_snapshot_incomplete():
    result = FakeLedgerRepository([Result(None)]).positions_tieout()

    assert result["incomplete"] is True
    assert result["source_status"] == {
        "exchange": "unavailable",
        "ledger": "not_read",
    }
    assert result["quantity_tieout"] == []


def test_positions_tieout_does_not_match_empty_sources():
    result = FakeLedgerRepository(
        [Result({"as_of_ms": 100}), Result([]), Result([])]
    ).positions_tieout()

    assert result["incomplete"] is True
    assert result["source_status"] == {"exchange": "empty", "ledger": "empty"}
    assert result["quantity_tieout"] == []


def test_tieout_marks_empty_closed_range_incomplete():
    result = FakeLedgerRepository([Result([]), Result([]), Result([])]).tieout(
        date(2026, 9, 30), date(2026, 9, 30)
    )

    assert result["incomplete"] is True
    assert result["source_status"] == {"exchange": "empty", "ledger": "empty"}
    assert result["days"][0]["status"] == "incomplete"
