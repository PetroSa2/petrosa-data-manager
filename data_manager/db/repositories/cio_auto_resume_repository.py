"""MongoDB persistence for CIO auto-resume entries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from data_manager.db.repositories.base_repository import BaseRepository

COLLECTION = "cio_auto_resume_registry"


class CioAutoResumeRepository(BaseRepository):
    """Store the durable registry used by CIO's auto-resume worker."""

    def _collection(self):
        if not self.mongodb or not getattr(self.mongodb, "db", None):
            raise RuntimeError("MongoDB is not available")
        return self.mongodb.db[COLLECTION]

    async def list_entries(
        self, status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        query = {"status": {"$in": status}} if status else {}
        rows = await self._collection().find(query).to_list(length=None)
        for row in rows:
            row.pop("_id", None)
        return rows

    async def get_entry(self, strategy_id: str) -> dict[str, Any] | None:
        row = await self._collection().find_one({"strategy_id": strategy_id})
        if row:
            row.pop("_id", None)
        return row

    async def upsert_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        document = dict(entry)
        document["updated_at"] = document.get("updated_at") or datetime.now(UTC)
        await self._collection().replace_one(
            {"strategy_id": document["strategy_id"]}, document, upsert=True
        )
        return (await self.get_entry(document["strategy_id"])) or document

    async def delete_entry(self, strategy_id: str) -> bool:
        result = await self._collection().delete_one({"strategy_id": strategy_id})
        return bool(result.deleted_count)
