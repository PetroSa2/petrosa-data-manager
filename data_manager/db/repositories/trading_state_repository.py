"""MongoDB repository for live positions and daily P&L state."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.base_repository import BaseRepository


class TradingStateConflictError(ValueError):
    pass


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

    async def _update(
        self, table: str, identity: dict[str, str], data: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.mongodb is None:
            raise RuntimeError("MongoDB is not available")
        if "_id" in data or any(
            key in data and data[key] != value for key, value in identity.items()
        ):
            raise TradingStateConflictError("trading state identity is immutable")
        if "id" in data:
            existing = await self._collection(table).find_one(identity)
            if existing and data["id"] != existing.get("id"):
                raise TradingStateConflictError("trading state identity is immutable")
        query: dict[str, Any] = dict(identity)
        update = {
            "$set": self.mongodb._prepare_for_bson({**self._public(data), **identity})
        }
        if os.getenv("PETROSA_TRADING_MYSQL_COPY_ENABLED", "true").lower() == "true":
            sequence = await self._collection(
                "trading_state_mysql_sequences"
            ).find_one_and_update(
                {"_id": f"{table}:{next(iter(identity.values()))}"},
                {"$inc": {"version": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            version = sequence["version"]
            update["$set"]["_mysql_copy_pending"] = True
            update["$set"]["_mysql_copy_attempted_at"] = datetime.min
            update["$set"]["_mysql_copy_version"] = version
            query["$or"] = [
                {"_mysql_copy_version": {"$lt": version}},
                {"_mysql_copy_version": {"$exists": False}},
            ]
        return query, update

    async def create_position(self, document: dict[str, Any]) -> bool:
        _, update = await self._update(
            "positions", {"position_id": document["position_id"]}, document
        )
        row = update["$set"]
        await self._collection("positions").insert_one(row)
        return True

    async def get_position(self, position_id: str) -> dict[str, Any] | None:
        doc = await self._collection("positions").find_one({"position_id": position_id})
        return self._public(doc) if doc else None

    async def update_position(
        self, position_id: str, data: dict[str, Any], *, upsert: bool = False
    ) -> int:
        query, update = await self._update(
            "positions", {"position_id": position_id}, data
        )
        try:
            result = await self._collection("positions").update_one(
                query, update, upsert=upsert
            )
        except DuplicateKeyError as exc:
            raise TradingStateConflictError("newer trading state write; retry") from exc
        changed = int(result.modified_count or result.upserted_id is not None)
        if not changed and "_mysql_copy_version" in update["$set"]:
            raise TradingStateConflictError("newer trading state write; retry")
        return changed

    async def close_position(self, position_id: str, data: dict[str, Any]) -> bool:
        query, update = await self._update(
            "positions", {"position_id": position_id}, {**data, "status": "closed"}
        )
        query["status"] = {"$in": ["open", "partially_closed"]}
        result = await self._collection("positions").update_one(query, update)
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
        query, update = await self._update("daily_pnl", {"date": date}, data)
        try:
            result = await self._collection("daily_pnl").update_one(
                query, update, upsert=True
            )
        except DuplicateKeyError as exc:
            raise TradingStateConflictError("newer trading state write; retry") from exc
        if (
            not (result.modified_count or result.upserted_id is not None)
            and "_mysql_copy_version" in update["$set"]
        ):
            raise TradingStateConflictError("newer trading state write; retry")
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

    async def pending_copy_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "positions": 0,
            "daily_pnl": 0,
            "oldest_attempted_at": None,
        }
        oldest: datetime | None = None
        for table in ("positions", "daily_pnl"):
            collection = self._collection(table)
            summary[table] = await collection.count_documents(
                {"_mysql_copy_pending": True}
            )
            row = await collection.find_one(
                {"_mysql_copy_pending": True},
                {"_mysql_copy_attempted_at": 1},
                sort=[("_mysql_copy_attempted_at", 1), ("_id", 1)],
            )
            attempted_at = row.get("_mysql_copy_attempted_at") if row else None
            if isinstance(attempted_at, datetime) and (
                oldest is None or attempted_at < oldest
            ):
                oldest = attempted_at
        summary["oldest_attempted_at"] = oldest.isoformat() if oldest else None
        return summary

    async def defer_copy(self, table: str, document: dict[str, Any]) -> None:
        key = "date" if table == "daily_pnl" else "position_id"
        await self._collection(table).update_one(
            {
                key: document[key],
                "_mysql_copy_version": document["_mysql_copy_version"],
            },
            {"$set": {"_mysql_copy_attempted_at": datetime.now(UTC)}},
        )
