"""Strategy timeline repository (P3.3, petrosa_k8s#600).

Joins five per-strategy sources into a single chronological event list:

  * ``strategy_config_audit`` — config-change rows persisted by
    :class:`ConfigurationRepository.upsert_strategy_config`.
  * ``characterizations`` — backtest characterization artifacts (P3.2,
    #599). The ``created_at`` field is the timeline timestamp.
  * ``strategy_lifecycle_events`` — CIO lifecycle events (ADMIT /
    PROMOTE / DEMOTE / RETIRE). The writer ships with P1.2; this
    repository reads the collection if it exists and returns empty
    rows otherwise. No coupling to the writer is required.
  * ``intents`` — CIO intent events.
  * ``cio_decisions`` — CIO decision events.
  * ``execution_events`` — order placements, fills, rejections.

Pagination is cursor-based on the ``(event_ts, event_id)`` pair so the
order is stable across heterogeneous sources. The cursor is an opaque
string a caller passes back unmodified; the repository encodes /
decodes it internally.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from data_manager.db.repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)


# Public event-type tags — used by the API's `types` filter.
TYPE_CONFIG = "config"
TYPE_CHARACTERIZATION = "characterization"
TYPE_LIFECYCLE = "lifecycle"
TYPE_INTENT = "intent"
TYPE_DECISION = "decision"
TYPE_EXECUTION = "execution"

ALL_TYPES = frozenset(
    {
        TYPE_CONFIG,
        TYPE_CHARACTERIZATION,
        TYPE_LIFECYCLE,
        TYPE_INTENT,
        TYPE_DECISION,
        TYPE_EXECUTION,
    }
)

# Internal map: type → (collection name, timestamp field, id field).
_SOURCE_MAP: dict[str, tuple[str, str, str]] = {
    TYPE_CONFIG: ("strategy_config_audit", "changed_at", "_id"),
    TYPE_CHARACTERIZATION: ("characterizations", "created_at", "_id"),
    TYPE_LIFECYCLE: ("strategy_lifecycle_events", "timestamp", "_id"),
    TYPE_INTENT: ("intents", "timestamp", "intent_id"),
    TYPE_DECISION: ("cio_decisions", "timestamp", "decision_id"),
    TYPE_EXECUTION: ("execution_events", "timestamp", "order_id"),
}


class StrategyTimelineRepository(BaseRepository):
    """Reads + merges per-strategy events across audit-trail collections."""

    async def get_timeline(
        self,
        *,
        strategy_id: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        types: list[str] | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return a chronologically-merged page of events for one strategy.

        ``cursor`` (when present) is the value returned as ``next_cursor``
        from the previous page; the caller passes it back verbatim.
        ``limit`` is clamped to 1..1000.
        """
        limit = max(1, min(int(limit), 1000))
        active_types = ALL_TYPES if not types else (set(types) & ALL_TYPES)
        if not active_types:
            return {"strategy_id": strategy_id, "events": [], "next_cursor": None}

        cursor_dt, cursor_id = _decode_cursor(cursor)
        if cursor_dt is not None:
            # When paginating, the effective lower bound is the cursor's
            # timestamp (we re-emit events strictly after `(cursor_dt, cursor_id)`).
            from_ts = cursor_dt if from_ts is None or cursor_dt > from_ts else from_ts

        # Fetch limit+2 per source so final-page detection survives the
        # cursor's post-filter (which can drop the row that anchored the
        # cursor when its ts is shared with other events).
        fetch_n = limit + 2
        collected: list[dict[str, Any]] = []
        for type_tag in active_types:
            rows = await self._fetch(type_tag, strategy_id, from_ts, to_ts, fetch_n)
            for row in rows:
                normalized = _normalise(type_tag, row)
                if normalized is None:
                    continue
                collected.append(normalized)

        # Sort by (ts asc, event_id asc) so the cursor walks forward
        # deterministically even when multiple sources land on the same
        # millisecond.
        collected.sort(key=_sort_key)

        # Skip past the cursor row if pagination was active.
        if cursor_dt is not None and cursor_id is not None:
            collected = [
                e
                for e in collected
                if (_event_ts(e), e["event_id"]) > (cursor_dt, cursor_id)
            ]

        page = collected[:limit]
        next_cursor = None
        if len(collected) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(_event_ts(last), last["event_id"])

        return {
            "strategy_id": strategy_id,
            "events": page,
            "next_cursor": next_cursor,
        }

    async def _fetch(
        self,
        type_tag: str,
        strategy_id: str,
        from_ts: datetime | None,
        to_ts: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self.mongodb or not getattr(self.mongodb, "db", None):
            return []
        collection_name, ts_field, _ = _SOURCE_MAP[type_tag]
        query: dict[str, Any] = {"strategy_id": strategy_id}
        ts_range: dict[str, Any] = {}
        if from_ts is not None:
            ts_range["$gte"] = from_ts
        if to_ts is not None:
            ts_range["$lt"] = to_ts
        if ts_range:
            query[ts_field] = ts_range

        try:
            # Both the collection lookup and the read happen inside the
            # try block — a missing collection (or any adapter error) is
            # treated as "no rows" rather than failing the whole page.
            collection = self.mongodb.db[collection_name]
            cursor = collection.find(query).sort(ts_field, 1).limit(limit)
            return await cursor.to_list(length=limit)
        except Exception as exc:
            logger.debug(
                "timeline: %s read failed (collection may not exist): %s",
                collection_name,
                exc,
            )
            return []


# ----------------------------------------------------------------------
# Normalisation + cursor helpers.
# ----------------------------------------------------------------------


def _normalise(type_tag: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """Project a raw collection row into the canonical timeline event shape."""
    _, ts_field, id_field = _SOURCE_MAP[type_tag]
    raw_ts = row.get(ts_field)
    if raw_ts is None:
        return None
    ts = _coerce_ts(raw_ts)
    if ts is None:
        return None
    event_id = row.get(id_field) or row.get("_id")
    if event_id is None:
        return None
    # Pop the internal Mongo _id so the payload is JSON-safe.
    payload = {k: v for k, v in row.items() if k != "_id"}
    return {
        "ts": ts.isoformat(),
        "type": type_tag,
        "source": _SOURCE_MAP[type_tag][0],
        "event_id": str(event_id),
        "payload": payload,
    }


def _coerce_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _event_ts(e: dict[str, Any]) -> datetime:
    return _coerce_ts(e["ts"]) or datetime.fromtimestamp(0, tz=UTC)


def _sort_key(e: dict[str, Any]) -> tuple[datetime, str]:
    return (_event_ts(e), e["event_id"])


def _encode_cursor(ts: datetime, event_id: str) -> str:
    blob = json.dumps({"ts": ts.isoformat(), "event_id": event_id})
    return base64.urlsafe_b64encode(blob.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(decoded)
        ts = _coerce_ts(payload.get("ts"))
        event_id = payload.get("event_id")
        return ts, event_id
    except (ValueError, json.JSONDecodeError, KeyError):
        # Malformed cursor: treat as if no cursor was provided. The
        # caller probably hand-crafted it; we don't crash the page.
        return None, None
