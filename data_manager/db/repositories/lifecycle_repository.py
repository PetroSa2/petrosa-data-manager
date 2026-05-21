"""Lifecycle reconstruction join (#603 P4.3).

Reconstructs the full lifecycle of a single CIO decision by joining the
four cross-service identifier collections established by P0.2:

  ``intents``  →  ``cio_decisions``  →  ``execution_events``  →  ``pnl_events``

All four collections key on ``decision_id``. ``cio_decisions.decision_id``
is unique (CIO has assigned the id by the time it publishes onto
``signals.trading.>``); ``intents.decision_id`` is sparse (publishers
may emit before CIO assigns); ``execution_events`` and ``pnl_events``
have non-unique ``decision_id`` (one decision produces many events over
time).

The reconstruction is intentionally read-only and ordered: each leg is
sorted chronologically so consumers (P4.4 "why did portfolio do X at
time T", P5.1 time slider) can scrub through the lifecycle without
re-sorting. ``_id`` is stripped from every document so the response is
JSON-safe.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from data_manager.db.repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)


INTENTS_COLLECTION = "intents"
CIO_DECISIONS_COLLECTION = "cio_decisions"
EXECUTION_EVENTS_COLLECTION = "execution_events"
PNL_EVENTS_COLLECTION = "pnl_events"


class LifecycleRepository(BaseRepository):
    """MongoDB-backed reconstruction across the four lifecycle collections."""

    async def reconstruct(self, decision_id: str) -> dict[str, Any] | None:
        """Reconstruct the full lifecycle for one ``decision_id``.

        Returns ``None`` when no ``cio_decisions`` row exists for the
        given id — the decision itself is the anchor; without it the
        join has no meaningful root (and the HTTP layer should serve
        404). If the decision exists but some legs are empty (no
        execution events yet, no P&L closed yet), those legs come back
        as empty lists / None and the caller surfaces that state
        verbatim — empty legs are signal, not error.
        """
        if self.mongodb is None or not getattr(self.mongodb, "db", None):
            logger.warning(
                "lifecycle_reconstruct_no_mongo",
                extra={"decision_id": decision_id},
            )
            return None

        decision = await self._fetch_one(CIO_DECISIONS_COLLECTION, decision_id)
        if decision is None:
            return None

        # Intents: sparse decision_id, so multiple intents may share one
        # decision OR an intent may exist with no decision_id at all
        # (publisher emitted before CIO assigned). For the join, we ask
        # for "intents that resolved to this decision_id".
        intents = await self._fetch_many(
            INTENTS_COLLECTION, decision_id, sort_field="timestamp"
        )
        executions = await self._fetch_many(
            EXECUTION_EVENTS_COLLECTION,
            decision_id,
            sort_field="timestamp",
        )
        pnl_events = await self._fetch_many(
            PNL_EVENTS_COLLECTION, decision_id, sort_field="timestamp"
        )

        return {
            "decision_id": decision_id,
            "decision": decision,
            "intents": intents,
            "executions": executions,
            "pnl_events": pnl_events,
            "summary": _lifecycle_summary(decision, executions, pnl_events),
        }

    async def reconstruct_by_strategy(
        self,
        strategy_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List reconstructions for every decision the strategy made.

        Used by P5.1's strategy lifecycle view. Picks up to ``limit``
        decisions, newest first, then reconstructs each. Time window
        applies to the decision's timestamp — not to individual leg
        events — so a long-running decision's late P&L still surfaces.
        """
        if self.mongodb is None or not getattr(self.mongodb, "db", None):
            return []

        try:
            decisions = await self.mongodb.find_filtered(
                CIO_DECISIONS_COLLECTION,
                filters={"strategy_id": strategy_id},
                start=start,
                end=end,
                limit=limit,
                sort_field="timestamp",
                sort_order=-1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "lifecycle_strategy_decisions_fetch_failed",
                extra={"strategy_id": strategy_id, "error": str(exc)},
            )
            return []

        results: list[dict[str, Any]] = []
        for dec in decisions:
            decision_id = dec.get("decision_id")
            if not decision_id:
                # Shouldn't happen (decision_id is the unique key for the
                # collection) but defend against drift.
                continue
            try:
                reconstruction = await self.reconstruct(decision_id)
                if reconstruction is not None:
                    results.append(reconstruction)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "lifecycle_strategy_per_decision_failed",
                    extra={"decision_id": decision_id, "error": str(exc)},
                )
        return results

    async def _fetch_one(self, collection: str, decision_id: str) -> dict | None:
        try:
            doc = await self.mongodb.db[collection].find_one(
                {"decision_id": decision_id}
            )
            if doc is None:
                return None
            doc = dict(doc)
            doc.pop("_id", None)
            return doc
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "lifecycle_fetch_one_failed",
                extra={
                    "collection": collection,
                    "decision_id": decision_id,
                    "error": str(exc),
                },
            )
            return None

    async def _fetch_many(
        self,
        collection: str,
        decision_id: str,
        *,
        sort_field: str = "timestamp",
    ) -> list[dict]:
        try:
            cursor = (
                self.mongodb.db[collection]
                .find({"decision_id": decision_id})
                .sort(sort_field, 1)
            )
            docs = await cursor.to_list(length=None)
            for d in docs:
                d.pop("_id", None)
            return docs
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "lifecycle_fetch_many_failed",
                extra={
                    "collection": collection,
                    "decision_id": decision_id,
                    "error": str(exc),
                },
            )
            return []


def _lifecycle_summary(
    decision: dict[str, Any],
    executions: list[dict[str, Any]],
    pnl_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive an at-a-glance summary for the lifecycle.

    Calculated fields:
      * ``action`` — verbatim from the decision.
      * ``executions_count`` / ``pnl_events_count``.
      * ``has_filled`` — True iff any execution event has
        ``event_type == "filled"``.
      * ``realized_pnl_usd`` — sum of ``realized_pnl_usd`` across closed
        P&L events; None when no closed events exist.

    The summary is best-effort: missing optional fields don't fail the
    reconstruction, they just leave the summary entry as None.
    """
    has_filled = any(
        (e.get("event_type") or "").lower() == "filled" for e in executions
    )
    realized = [
        float(p.get("realized_pnl_usd"))
        for p in pnl_events
        if (p.get("pnl_kind") or "").lower() == "closed"
        and p.get("realized_pnl_usd") is not None
    ]
    return {
        "action": decision.get("action"),
        "strategy_id": decision.get("strategy_id"),
        "decision_timestamp": _maybe_iso(decision.get("timestamp")),
        "executions_count": len(executions),
        "pnl_events_count": len(pnl_events),
        "has_filled": has_filled,
        "realized_pnl_usd": sum(realized) if realized else None,
    }


def _maybe_iso(value: Any) -> str | None:
    """Coerce a datetime field to ISO 8601 — leaves strings/None alone."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None
