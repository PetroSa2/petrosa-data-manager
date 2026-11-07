"""The precomputed exchange-truth stats snapshots (petrosa-data-manager#842).

The refresh job writes one document per cycle to ``strategy_stats_snapshots`` (TTL on ``computed_at``, registered in
the persistence registry); the API serves the latest one. Staleness is decided here, not by the TTL: a snapshot older
than ``STALE_AFTER_CYCLES`` refresh intervals is reported as stale, and the serving layer then answers "unavailable"
instead of old numbers.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

COLLECTION = "strategy_stats_snapshots"
#: Labelled fallback: the refresh cadence (15 min).
REFRESH_INTERVAL_SECONDS = float(
    os.getenv("STRATEGY_STATS_REFRESH_INTERVAL_SECONDS", "900")
)
#: A snapshot is stale after this many missed refresh cycles.
STALE_AFTER_CYCLES = 2


def stale_after_seconds() -> float:
    return STALE_AFTER_CYCLES * REFRESH_INTERVAL_SECONDS


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def is_stale(snapshot: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """True when there is no snapshot, its time is unreadable, or it is older than two refresh cycles."""
    if not snapshot:
        return True
    computed_at = _as_utc(snapshot.get("computed_at"))
    if computed_at is None:
        return True
    now = now or datetime.now(UTC)
    return now - computed_at > timedelta(seconds=stale_after_seconds())


class StrategyStatsSnapshotStore:
    """Write and read the snapshots through the data-manager's MongoDB adapter."""

    def __init__(self, mongodb_adapter: Any) -> None:
        self.mongodb = mongodb_adapter
        self._indexes_ensured = False

    async def _ensure_indexes(self) -> None:
        # A new collection gets its registered TTL index before the first write, not at the next restart.
        if not self._indexes_ensured:
            await self.mongodb.ensure_indexes(COLLECTION)
            self._indexes_ensured = True

    async def save(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot.get("computed_at"), datetime):
            raise ValueError("snapshot needs a datetime computed_at (the TTL field)")
        await self._ensure_indexes()
        await self.mongodb.db[COLLECTION].insert_one(dict(snapshot))

    async def latest(self) -> dict[str, Any] | None:
        row = await self.mongodb.db[COLLECTION].find_one({}, sort=[("computed_at", -1)])
        if not row:
            return None
        row = dict(row)
        row.pop("_id", None)
        return row
