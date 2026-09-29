"""Persistence for operational strategy lifecycle events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from data_manager.db.repositories.base_repository import BaseRepository

COLLECTION = "strategy_lifecycle_events"


class StrategyLifecycleRepository(BaseRepository):
    """Store and retrieve lifecycle transitions for a strategy."""

    def _collection(self):
        if self.mongodb is None or getattr(self.mongodb, "db", None) is None:
            raise RuntimeError("MongoDB is not available")
        return self.mongodb.db[COLLECTION]

    async def insert_event(self, event: dict[str, Any]) -> dict[str, Any]:
        document = dict(event)
        result = await self._collection().insert_one(document)
        return {"event_id": str(result.inserted_id), **document}

    async def get_state(self, strategy_id: str) -> dict[str, Any] | None:
        return await self._collection().find_one(
            {"strategy_id": strategy_id}, sort=[("transitioned_at", -1)]
        )

    async def get_events(
        self, strategy_id: str, limit: int, order: str
    ) -> list[dict[str, Any]]:
        cursor = (
            self._collection()
            .find({"strategy_id": strategy_id})
            .sort("transitioned_at", 1 if order == "asc" else -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)


def utc_datetime(value: datetime | None) -> datetime:
    """Return a timezone-aware UTC datetime for Mongo persistence."""
    value = value or datetime.now(UTC)
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def event_id(document: dict[str, Any]) -> str | None:
    value = document.get("_id") or document.get("event_id")
    return str(value) if value is not None else None
