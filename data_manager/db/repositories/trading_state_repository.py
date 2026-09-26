"""MongoDB repository for live positions and daily P&L state."""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.base_repository import BaseRepository


class TradingStateRepository(BaseRepository):
    """CRUD operations for the operational trading state collections."""

    def _collection(self, name: str):
        if self.mongodb is None or self.mongodb.db is None:
            raise RuntimeError("MongoDB is not available")
        return self.mongodb.db[name]

    async def create_position(self, document: dict[str, Any]) -> bool:
        await self._collection("positions").insert_one(
            self.mongodb._prepare_for_bson(dict(document))
        )
        return True

    async def get_position(self, position_id: str) -> dict[str, Any] | None:
        doc = await self._collection("positions").find_one({"position_id": position_id})
        if doc:
            doc.pop("_id", None)
        return doc

    async def update_position(
        self, position_id: str, data: dict[str, Any], *, upsert: bool = False
    ) -> int:
        result = await self._collection("positions").update_one(
            {"position_id": position_id},
            {"$set": self.mongodb._prepare_for_bson(dict(data))},
            upsert=upsert,
        )
        return int(result.modified_count or result.upserted_id is not None)

    async def close_position(self, position_id: str, data: dict[str, Any]) -> bool:
        result = await self._collection("positions").update_one(
            {
                "position_id": position_id,
                "status": {"$in": ["open", "partially_closed"]},
            },
            {"$set": self.mongodb._prepare_for_bson({**data, "status": "closed"})},
        )
        return bool(result.modified_count)

    async def close_by_side(
        self, symbol: str, position_side: str, data: dict[str, Any]
    ) -> str | None:
        doc = await self._collection("positions").find_one(
            {
                "symbol": symbol,
                "position_side": position_side,
                "status": {"$in": ["open", "partially_closed"]},
            },
            sort=[("entry_time", DESCENDING)],
        )
        if not doc:
            return None
        await self.close_position(str(doc["position_id"]), data)
        return str(doc["position_id"])

    async def list_positions(
        self, filters: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        cursor = (
            self._collection("positions")
            .find(filters)
            .sort("entry_time", DESCENDING)
            .limit(limit)
        )
        rows = await cursor.to_list(length=limit)
        for row in rows:
            row.pop("_id", None)
        return rows

    async def get_daily_pnl(self, date: str) -> dict[str, Any] | None:
        doc = await self._collection("daily_pnl").find_one({"date": date})
        if doc:
            doc.pop("_id", None)
        return doc

    async def put_daily_pnl(self, date: str, data: dict[str, Any]) -> dict[str, Any]:
        await self._collection("daily_pnl").update_one(
            {"date": date},
            {"$set": self.mongodb._prepare_for_bson({"date": date, **data})},
            upsert=True,
        )
        return (await self.get_daily_pnl(date)) or {"date": date, **data}
