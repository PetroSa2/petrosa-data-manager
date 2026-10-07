"""Cost-aware strategy scorecard routes (petrosa-data-manager#468). Read-only.

``GET /analysis/scorecard`` (also under ``/api/v1/analysis``), ``/scorecard/evaluate`` and the keep/kill input of
the CIO, ``/strategy-net-r`` (petrosa-cio#299). Computed from the round book over ``execution_events``; the stop a
position carried comes from its ``positions`` row, the CIO mode from ``cio_decisions`` (a decision older than that
collection's retention reads as mode ``unknown``).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import data_manager.api.app as api_module
from data_manager.services.round_book import FILL_EVENT_TYPES, RoundBook, _when
from data_manager.services.scorecard import (
    GROUP_BYS,
    ScoredRound,
    evaluate,
    in_period,
    score_rounds,
    scorecard,
    stop_fraction_of,
    strategy_net_r,
)

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


async def _scored(end: datetime | None) -> tuple[list[ScoredRound], RoundBook]:
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
    return score_rounds(book.closed, stops=stops, decisions=decisions), book


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
    rounds, book = await _scored(to)
    selected = in_period(rounds, from_, to)
    body = scorecard(
        selected,
        group_by,
        _int_env("SCORECARD_MIN_TRADES"),
        _open_counts(book, group_by),
    )
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
    rounds, book = await _scored(to)
    minimum = _int_env("SCORECARD_MIN_TRADES")
    card = scorecard(
        in_period(rounds, from_, to), group_by, minimum, _open_counts(book, group_by)
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
    rounds, _ = await _scored(None)
    return strategy_net_r(rounds)
