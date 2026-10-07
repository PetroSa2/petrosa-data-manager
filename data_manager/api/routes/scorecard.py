"""Cost-aware strategy scorecard routes (petrosa-data-manager#468). Read-only.

``GET /analysis/scorecard`` (also under ``/api/v1/analysis``), ``/scorecard/evaluate`` and the keep/kill input of
the CIO, ``/strategy-net-r`` (petrosa-cio#299). Computed from the round book over ``execution_events``; the stop a
position carried comes from its ``positions`` row, the CIO mode from ``cio_decisions`` (a decision older than that
collection's retention reads as mode ``unknown``). Each round's net also carries its funding share and the
response reconciles with the ledger of the period (petrosa-data-manager#556).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.db.repositories.ledger_repository import LedgerRepository
from data_manager.services.round_book import (
    FILL_EVENT_TYPES,
    OpenRound,
    RoundBook,
    _when,
)
from data_manager.services.scorecard import (
    GROUP_BYS,
    ScoredRound,
    evaluate,
    in_period,
    s as dec_str,
    score_rounds,
    scorecard,
    stop_fraction_of,
    strategy_net_r,
)
from data_manager.services.scorecard_funding import (
    MARK_HOURS,
    Exposure,
    FundingAllocation,
    allocate_funding,
)
from data_manager.services.scorecard_ledger import period_ledger

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_FILLS = 200_000


async def _load_fills(end: datetime | None) -> list[dict[str, Any]]:
    mongodb = api_module.db_manager.mongodb_adapter
    query: dict[str, Any] = {"event_type": {"$in": sorted(FILL_EVENT_TYPES)}}
    if end is not None:
        query["timestamp"] = {"$lt": end}
    return (
        await mongodb.db["execution_events"]
        .find(query)
        .sort("timestamp", 1)
        .to_list(length=_MAX_FILLS)
    )


async def _load_stops(position_ids: list[str]) -> dict[str, Decimal]:
    """position id -> the stop fraction the position carried, from its row."""
    if not position_ids:
        return {}
    mongodb = api_module.db_manager.mongodb_adapter
    rows = (
        await mongodb.db["positions"]
        .find({"position_id": {"$in": position_ids}})
        .to_list(length=len(position_ids))
    )
    out: dict[str, Decimal] = {}
    for row in rows:
        stop = stop_fraction_of(
            row.get("entry_price") or row.get("avg_price"), row.get("stop_loss")
        )
        if stop is not None and row.get("position_id"):
            out[str(row["position_id"])] = stop
    return out


async def _load_decisions(decision_ids: list[str]) -> dict[str, dict[str, Any]]:
    """decision id -> {"source", "stop_fraction"} from ``cio_decisions``."""
    if not decision_ids:
        return {}
    mongodb = api_module.db_manager.mongodb_adapter
    rows = (
        await mongodb.db["cio_decisions"]
        .find({"decision_id": {"$in": decision_ids}})
        .to_list(length=len(decision_ids))
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row.get("payload") or {}
        price = payload.get("entry_price") or payload.get("price") or row.get("price")
        stop = stop_fraction_of(price, payload.get("stop_loss"))
        if stop is None and payload.get("stop_loss_pct") is not None:
            try:
                stop = Decimal(str(payload["stop_loss_pct"]))
            except InvalidOperation:
                stop = None
        out[str(row["decision_id"])] = {
            "source": row.get("source"),
            "stop_fraction": stop,
        }
    return out


@dataclass
class Scored:
    """The scored closed rounds with what the funding allocation and the ledger reconciliation need."""

    rounds: list[ScoredRound]
    book: RoundBook
    open_rounds: list[OpenRound]
    funding_income: dict[tuple[str, date], Decimal] | None
    allocation: FundingAllocation | None
    days_missing: list[str]
    funding_error: str | None


async def _load_funding(
    first: date, last: date
) -> tuple[dict[tuple[str, date], Decimal], set[date]]:
    """Exchange funding income per (symbol, day) and the days that have a ledger revision."""
    manager = api_module.db_manager
    if not manager or not getattr(manager, "mysql_adapter", None):
        raise RuntimeError("MySQL is unavailable")
    repository = LedgerRepository(manager.mysql_adapter, None)
    return await asyncio.to_thread(repository.funding_by_symbol_day, first, last)


async def _scored(start: datetime | None, end: datetime | None) -> Scored:
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    try:
        fills = await _load_fills(end)
        fills.sort(key=lambda row: _when(row) or datetime.min.replace(tzinfo=UTC))
        book = RoundBook()
        for row in fills:
            book.apply(row)
        position_ids = sorted({p for r in book.closed for p in r.position_ids})
        decision_ids = sorted({d for r in book.closed for d in r.decision_ids})
        stops = await _load_stops(position_ids)
        decisions = await _load_decisions(decision_ids)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("scorecard: read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    open_rounds = book.open_cycles()
    exposures = [
        Exposure(
            i, r.symbol, r.opened_at, r.closed_at, r.entry_notional, r.position_side
        )
        for i, r in enumerate(book.closed)
    ] + [
        Exposure(
            len(book.closed) + j,
            o.symbol,
            o.opened_at,
            None,
            o.entry_notional,
            o.position_side,
        )
        for j, o in enumerate(open_rounds)
    ]
    funding_income: dict[tuple[str, date], Decimal] | None = None
    allocation: FundingAllocation | None = None
    days_missing: list[str] = []
    funding_error: str | None = None
    opened = [e.opened_at for e in exposures] + ([start] if start else [])
    if opened:
        first = min(opened).astimezone(UTC).date()
        last = (end or datetime.now(UTC)).astimezone(UTC).date()
        try:
            funding_income, present = await _load_funding(first, last)
            allocation = allocate_funding(exposures, funding_income)
            days_missing = [
                (first + timedelta(days=i)).isoformat()
                for i in range((last - first).days + 1)
                if (first + timedelta(days=i)) not in present
            ]
        except Exception as exc:  # the scorecard still serves, without funding
            logger.warning("scorecard: funding unavailable: %s", exc)
            funding_error = str(exc)
    else:
        funding_income, allocation = {}, FundingAllocation()
    income = allocation.income_by_round() if allocation else {}
    funding_cost = {i: -income.get(i, Decimal(0)) for i in range(len(book.closed))}
    scored = score_rounds(
        book.closed, stops=stops, decisions=decisions, funding_cost=funding_cost
    )
    return Scored(
        scored,
        book,
        open_rounds,
        funding_income,
        allocation,
        days_missing,
        funding_error,
    )


def _funding_block(data: Scored, ledger: dict[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "status": ledger["funding"]["status"],
        "source": "ledger_exchange_daily, latest revision of each day",
        "method": (
            "funding is charged to rounds open at the 00:00/08:00/16:00 UTC marks, pro rata by entry notional x "
            "marks held, per symbol and day; what no known round held is unallocated"
        ),
        "marks_utc": list(MARK_HOURS),
        "days_missing": data.days_missing,
        "hedged_symbol_days": len(data.allocation.hedged_symbol_days)
        if data.allocation
        else None,
        **{k: v for k, v in ledger["funding"].items() if k != "status"},
    }
    if data.funding_error:
        block["error"] = data.funding_error
    return block


def _decimal_env(name: str) -> Decimal | None:
    try:
        return Decimal(os.environ[name])
    except (KeyError, InvalidOperation):
        return None


def _int_env(name: str) -> int | None:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return None


def _open_counts(book: RoundBook, group_by: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for strategy_id, symbol in book.open_rounds():
        key = (
            f"{strategy_id}/{symbol}" if group_by == "strategy_symbol" else strategy_id
        )
        if group_by != "cio_mode":
            counts[key] = counts.get(key, 0) + 1
    return counts


@router.get("/scorecard")
async def get_scorecard(
    from_: datetime | None = Query(
        None, alias="from", description="Rounds closed at or after this time (UTC)"
    ),
    to: datetime | None = Query(
        None, description="Rounds closed before this time (UTC)"
    ),
    group_by: str = Query("strategy", pattern="^(strategy|strategy_symbol|cio_mode)$"),
) -> dict[str, Any]:
    """Metrics per group, net of fees, by the close time of each round. Open rounds are counted apart."""
    assert group_by in GROUP_BYS
    data = await _scored(from_, to)
    selected = in_period(data.rounds, from_, to)
    body = scorecard(
        selected,
        group_by,
        _int_env("SCORECARD_MIN_TRADES"),
        _open_counts(data.book, group_by),
    )
    ledger = period_ledger(
        closed=data.rounds,
        selected=selected,
        open_rounds=data.open_rounds,
        unattributed=data.book.unattributed_fills,
        start=from_,
        end=to,
        funding_income=data.funding_income,
        allocation=data.allocation,
    )
    group = ledger.pop("unattributed_group")
    body["groups_net"] = body["total_net"]
    body["unattributed"] = group
    body["total_net"] = ledger["total_net"]
    body["total_fees"] = dec_str(Decimal(body["total_fees"]) + Decimal(group["fees"]))
    body["total_funding"] = ledger["funding"]["exchange_total_cost"]
    body["open_entry_fee_delta"] = ledger["open_entry_fee_delta"]
    body["funding"] = _funding_block(data, ledger)
    ledger.pop("funding")
    body["conservation"] = ledger
    body["period"] = {
        "from": from_.isoformat() if from_ else None,
        "to": to.isoformat() if to else None,
    }
    return body


@router.get("/scorecard/evaluate")
async def get_scorecard_evaluation(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    group_by: str = Query("strategy", pattern="^(strategy|strategy_symbol|cio_mode)$"),
    capital: Decimal | None = Query(
        None, description="Capital the drawdown fraction is measured against"
    ),
) -> dict[str, Any]:
    """keep / watch / disable per group against configured thresholds; only reports, never changes state."""
    data = await _scored(from_, to)
    minimum = _int_env("SCORECARD_MIN_TRADES")
    card = scorecard(
        in_period(data.rounds, from_, to),
        group_by,
        minimum,
        _open_counts(data.book, group_by),
    )
    return evaluate(
        card["groups"],
        min_trades=minimum,
        min_expectancy_net=_decimal_env("SCORECARD_MIN_EXPECTANCY_NET"),
        max_dd_fraction=_decimal_env("SCORECARD_MAX_DD_FRACTION"),
        capital=capital,
    )


@router.get("/strategy-net-r")
async def get_strategy_net_r() -> dict[str, Any]:
    """Per strategy: net R per closed round (oldest first), the cumulative net loss and the closed-round count:
    the keep/kill input of the CIO (petrosa-cio#299)."""
    data = await _scored(None, None)
    return strategy_net_r(data.rounds)
