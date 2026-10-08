"""The keep/kill input of the CIO: net R per closed round of each strategy (petrosa-data-manager#468).

``GET /analysis/strategy-net-r`` (also under ``/api/v1/analysis``) is what the CIO's keep/kill job reads
(petrosa-cio#299, #306). Read-only: the rounds come from the round book over ``execution_events``, the stop a
position carried from its ``positions`` row, else from its ``cio_decisions`` row. The scorecard routes are the
drain's ``scorecard_service`` (``analysis.py``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException

import data_manager.api.app as api_module
from data_manager.services.round_book import FILL_EVENT_TYPES, RoundBook, _when
from data_manager.services.strategy_net_r import (
    score_rounds,
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


async def _scored() -> list:
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    try:
        fills = await _load_fills(None)
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
        logger.error("strategy-net-r: read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return score_rounds(book.closed, stops=stops, decisions=decisions)


@router.get("/strategy-net-r")
async def get_strategy_net_r() -> dict[str, Any]:
    """Per strategy: net R per closed round (oldest first), the cumulative net loss and the closed-round count:
    the keep/kill input of the CIO (petrosa-cio#299)."""
    return strategy_net_r(await _scored())
