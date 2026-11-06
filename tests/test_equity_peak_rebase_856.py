"""Equity-peak re-base for the drawdown steps (petrosa-data-manager#856)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.api.app import create_app
from data_manager.api.routes import risk as risk_route
from data_manager.services import risk_inputs as ri

START = date(2026, 10, 1)
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def _rows(balances: list[float]) -> list[dict]:
    """One ledger close per day from START, each taken at 00:05Z of the next day."""
    rows = []
    for i, balance in enumerate(balances):
        day = START + timedelta(days=i)
        taken = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
        rows.append(
            {
                "day": day,
                "wallet_balance": str(balance),
                "transfer": "0",
                "balance_as_of_ms": int(
                    (taken + timedelta(minutes=5)).timestamp() * 1000
                ),
            }
        )
    return rows


# A peak of 10190 on day 4, then a slide to ~9620, with daily noise.
BALANCES = [10080, 10084, 10083, 10083, 10190, 10181, 10060, 9991, 9994, 9998] + [
    9990 - 10 * i + (5 if i % 2 else -5) for i in range(30)
]
REBASE_AT = "2026-10-20T19:50:00+00:00"  # between the closes of 10-19 and 10-20


def _rebase(peak: float = 9950.0, at: str = REBASE_AT) -> dict:
    return {"adjustment_id": "adj-1", "peak": str(peak), "peak_at": at}


def test_without_a_rebase_the_curve_is_unchanged() -> None:
    curve = ri.equity_curve(_rows(BALANCES), window_days=30, now=NOW)
    assert curve["peak"] == 10190.0
    assert curve["peak_at"].startswith("2026-10-06T00:05")
    assert curve["peak_rebase"] is None


def test_rebase_sets_the_peak_and_ignores_earlier_higher_closes() -> None:
    curve = ri.equity_curve(_rows(BALANCES), window_days=30, now=NOW, rebase=_rebase())
    # every close after 10-20T19:50Z is below 9950, the 10190 close of 10-05 no longer counts
    assert curve["peak"] == 9950.0
    assert curve["peak_at"] == REBASE_AT
    assert curve["peak_rebase"] == {
        "adjustment_id": "adj-1",
        "peak": 9950.0,
        "peak_at": REBASE_AT,
    }


def test_a_later_higher_close_raises_the_rebased_peak() -> None:
    balances = BALANCES[:25] + [9990.0] + BALANCES[26:]
    curve = ri.equity_curve(_rows(balances), window_days=30, now=NOW, rebase=_rebase())
    assert curve["peak"] == 9990.0
    assert curve["peak_at"].startswith("2026-10-27T00:05")  # the close of day index 25


def test_sigma_and_balance_are_identical_with_and_without_a_rebase() -> None:
    plain = ri.equity_curve(_rows(BALANCES), window_days=30, now=NOW)
    based = ri.equity_curve(_rows(BALANCES), window_days=30, now=NOW, rebase=_rebase())
    for key in ("sigma_daily", "n_returns", "sufficient", "wallet_balance", "days"):
        assert based[key] == plain[key], key


def test_the_next_drawdown_is_measured_from_the_new_peak() -> None:
    curve = ri.equity_curve(_rows(BALANCES), window_days=30, now=NOW, rebase=_rebase())
    last = curve["wallet_balance"]
    drawdown = (curve["peak"] - last) / curve["peak"]
    assert drawdown == pytest.approx((9950.0 - last) / 9950.0)
    assert drawdown < (10190.0 - last) / 10190.0


@pytest.mark.parametrize(
    "bad",
    [
        {"peak": "nan", "peak_at": REBASE_AT},
        {"peak": "-1", "peak_at": REBASE_AT},
        {"peak": "9800", "peak_at": "not a time"},
        {"peak_at": REBASE_AT},
    ],
)
def test_an_invalid_rebase_keeps_the_unrebased_peak(bad: dict) -> None:
    curve = ri.equity_curve(_rows(BALANCES), window_days=30, now=NOW, rebase=bad)
    assert curve["peak"] == 10190.0
    assert curve["peak_rebase"] is None


# --- the endpoint ------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    async def candles(symbol, timeframe, start, end, db_manager=None):
        return []

    async def wallet(first, last, db_manager=None):
        return _rows(BALANCES)

    async def stored_peak(db_manager=None):
        return None

    monkeypatch.setattr(risk_route, "_load_candles", candles)
    monkeypatch.setattr(risk_route, "_load_wallet_rows", wallet)
    monkeypatch.setattr(risk_route, "_load_stored_peak", stored_peak)
    api_module.db_manager = MagicMock()
    try:
        yield TestClient(create_app())
    finally:
        api_module.db_manager = None


def _equity(client) -> dict:
    response = client.get("/api/v1/risk/inputs?symbols=BTCUSDT&window_days=30")
    assert response.status_code == 200
    return response.json()["equity"]


def test_endpoint_serves_the_latest_applied_rebase(client, monkeypatch) -> None:
    async def rebase(db_manager=None):
        return _rebase()

    monkeypatch.setattr(risk_route, "_load_peak_rebase", rebase)
    equity = _equity(client)
    assert equity["peak"] == 9950.0
    assert equity["peak_at"] == REBASE_AT
    assert equity["peak_rebase"]["adjustment_id"] == "adj-1"
    assert "peak_rebase_error" not in equity


def test_endpoint_fails_closed_to_the_unrebased_peak_when_the_record_is_unreadable(
    client, monkeypatch
) -> None:
    async def broken(db_manager=None):
        raise risk_route.PeakRebaseUnavailable("mysql down")

    monkeypatch.setattr(risk_route, "_load_peak_rebase", broken)
    equity = _equity(client)
    # never "no peak" (tradeengine would then take current equity as the peak): the higher one
    assert equity["peak"] == 10190.0
    assert equity["peak_rebase"] is None
    assert equity["peak_rebase_error"] == "peak_rebase_unavailable"


def test_endpoint_flags_an_invalid_rebase_record(client, monkeypatch) -> None:
    async def invalid(db_manager=None):
        return {"adjustment_id": "adj-2", "peak": "nan", "peak_at": REBASE_AT}

    monkeypatch.setattr(risk_route, "_load_peak_rebase", invalid)
    equity = _equity(client)
    assert equity["peak"] == 10190.0
    assert equity["peak_rebase_error"] == "peak_rebase_invalid"


def _repo_returning(monkeypatch, rows):
    class _Repo:
        def __init__(self, *args, **kwargs):
            pass

        def list_adjustments(self, **kwargs):
            if isinstance(rows, Exception):
                raise rows
            assert kwargs == {
                "target_table": "risk_equity_peak",
                "target_key": "portfolio",
            }
            return rows

    import data_manager.db.repositories.ledger_adjustments_repository as repo_module

    monkeypatch.setattr(repo_module, "LedgerAdjustmentsRepository", _Repo)


@pytest.mark.asyncio
async def test_loader_takes_the_last_applied_row_and_skips_dry_runs(
    monkeypatch,
) -> None:
    after_1 = {"peak": "9900", "peak_at": "2026-11-01T00:00:00+00:00"}
    after_2 = {"peak": "9623.15", "peak_at": "2026-11-06T20:00:00+00:00"}
    _repo_returning(
        monkeypatch,
        [
            {"adjustment_id": "a1", "run_mode": "apply", "after": after_1},
            {"adjustment_id": "d2", "run_mode": "dry_run", "after": {"peak": "1"}},
            {"adjustment_id": "a2", "run_mode": "apply", "after": after_2},
            {"adjustment_id": "d3", "run_mode": "dry_run", "after": {"peak": "2"}},
        ],
    )
    loaded = await risk_route._load_peak_rebase(MagicMock())
    assert loaded == {**after_2, "adjustment_id": "a2"}


@pytest.mark.asyncio
async def test_loader_returns_none_when_never_rebased_and_raises_on_failures(
    monkeypatch,
) -> None:
    _repo_returning(
        monkeypatch, [{"adjustment_id": "d1", "run_mode": "dry_run", "after": {}}]
    )
    assert await risk_route._load_peak_rebase(MagicMock()) is None

    _repo_returning(monkeypatch, RuntimeError("mysql down"))
    with pytest.raises(risk_route.PeakRebaseUnavailable) as err:
        await risk_route._load_peak_rebase(MagicMock())
    assert "mysql down" in str(err.value)

    _repo_returning(
        monkeypatch, [{"adjustment_id": "a1", "run_mode": "apply", "after": {}}]
    )
    with pytest.raises(risk_route.PeakRebaseUnavailable) as err:
        await risk_route._load_peak_rebase(MagicMock())
    assert "after.peak" in str(err.value)
