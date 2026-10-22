"""MongoDB repository for live positions and daily P&L state."""

from __future__ import annotations

import os
from datetime import UTC, datetime
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

    @staticmethod
    def _public(document: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in document.items()
            if key != "_id" and not key.startswith("_mysql_copy_")
        }

    def _update(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.mongodb is None:
            raise RuntimeError("MongoDB is not available")
        update = {"$set": self.mongodb._prepare_for_bson(self._public(data))}
        if os.getenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true").lower() == "true":
            update["$set"]["_mysql_copy_pending"] = True
            update["$set"]["_mysql_copy_attempted_at"] = datetime.min
            update["$inc"] = {"_mysql_copy_version": 1}
        return update

    async def create_position(self, document: dict[str, Any]) -> bool:
        update = self._update(document)
        row = update["$set"]
        if "$inc" in update:
            row["_mysql_copy_version"] = 1
        await self._collection("positions").insert_one(row)
        return True

    async def get_position(self, position_id: str) -> dict[str, Any] | None:
        doc = await self._collection("positions").find_one({"position_id": position_id})
        return self._public(doc) if doc else None

    async def update_position(
        self, position_id: str, data: dict[str, Any], *, upsert: bool = False
    ) -> int:
        result = await self._collection("positions").update_one(
            {"position_id": position_id},
            self._update(data),
            upsert=upsert,
        )
        return int(result.modified_count or result.upserted_id is not None)

    async def close_position(self, position_id: str, data: dict[str, Any]) -> bool:
        result = await self._collection("positions").update_one(
            {
                "position_id": position_id,
                "status": {"$in": ["open", "partially_closed"]},
            },
            self._update({**data, "status": "closed"}),
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
        return [self._public(row) for row in rows]

    async def get_daily_pnl(self, date: str) -> dict[str, Any] | None:
        doc = await self._collection("daily_pnl").find_one({"date": date})
        return self._public(doc) if doc else None

    async def put_daily_pnl(self, date: str, data: dict[str, Any]) -> dict[str, Any]:
        await self._collection("daily_pnl").update_one(
            {"date": date},
            self._update({"date": date, **data}),
            upsert=True,
        )
        return (await self.get_daily_pnl(date)) or {"date": date, **data}

    async def pending_copies(self, table: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await (
            self._collection(table)
            .find({"_mysql_copy_pending": True})
            .sort([("_mysql_copy_attempted_at", 1), ("_id", 1)])
            .limit(limit)
            .to_list(length=limit)
        )
        return rows

    async def acknowledge_copy(self, table: str, document: dict[str, Any]) -> None:
        key = "date" if table == "daily_pnl" else "position_id"
        await self._collection(table).update_one(
            {
                key: document[key],
                "_mysql_copy_version": document["_mysql_copy_version"],
            },
            {"$set": {"_mysql_copy_pending": False}},
        )

    async def pending_copy_count(self) -> int:
        return sum(
            [
                await self._collection(table).count_documents(
                    {"_mysql_copy_pending": True}
                )
                for table in ("positions", "daily_pnl")
            ]
        )

    async def defer_copy(self, table: str, document: dict[str, Any]) -> None:
        key = "date" if table == "daily_pnl" else "position_id"
        await self._collection(table).update_one(
            {
                key: document[key],
                "_mysql_copy_version": document["_mysql_copy_version"],
            },
            {"$set": {"_mysql_copy_attempted_at": datetime.now(UTC)}},
        )
