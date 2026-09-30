"""Atomic MongoDB-backed service leases."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.observability.write_metrics import SUMMARY, record_write

LEASES_COLLECTION = "service_leases"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class LeaseRepository(BaseRepository):
    """Repository for short-lived, fencing-token based service leases."""

    def __init__(self, mongodb_adapter=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(mysql_adapter=None, mongodb_adapter=mongodb_adapter)

    def _collection(self):  # type: ignore[no-untyped-def]
        if self.mongodb is None or self.mongodb.db is None:
            raise RuntimeError("LeaseRepository requires a connected MongoDB adapter")
        return self.mongodb.db[LEASES_COLLECTION]

    @staticmethod
    def _document(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if doc is None:
            return None
        return {
            "name": doc["name"],
            "owner": doc["owner"],
            "acquired_at": _utc(doc.get("acquired_at")),
            "expires_at": _utc(doc["expires_at"]),
            "renewed_at": _utc(doc.get("renewed_at")),
            "fencing_token": int(doc.get("fencing_token", 0)),
        }

    async def acquire(self, name: str, owner: str, ttl_s: int) -> dict[str, Any]:
        """Acquire or renew a lease, returning the current holder document."""
        started = time.monotonic()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_s)
        query = {
            "name": name,
            "$or": [{"expires_at": {"$lt": now}}, {"owner": owner}],
        }
        update = [
            {
                "$set": {
                    "name": name,
                    "owner": owner,
                    "acquired_at": {
                        "$cond": [{"$eq": ["$owner", owner]}, "$acquired_at", now]
                    },
                    "expires_at": expires_at,
                    "renewed_at": now,
                    "fencing_token": {"$add": [{"$ifNull": ["$fencing_token", 0]}, 1]},
                }
            }
        ]
        try:
            doc = await self._collection().find_one_and_update(
                query, update, upsert=True, return_document=ReturnDocument.AFTER
            )
            result = {"acquired": True, **self._document(doc)}
            outcome = "success"
        except DuplicateKeyError:
            holder = await self._collection().find_one({"name": name})
            result = {"acquired": False, **(self._document(holder) or {"name": name})}
            outcome = "duplicate"
        SUMMARY.lease("acquired" if result["acquired"] else "contended")
        record_write(LEASES_COLLECTION, outcome, "acquire", time.monotonic() - started)
        return result

    async def renew(self, name: str, owner: str, ttl_s: int) -> dict[str, Any] | None:
        """Renew a currently-held lease; return ``None`` when ownership is lost."""
        started = time.monotonic()
        now = datetime.now(UTC)
        doc = await self._collection().find_one_and_update(
            {"name": name, "owner": owner, "expires_at": {"$gte": now}},
            {"$set": {"expires_at": now + timedelta(seconds=ttl_s), "renewed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        result = self._document(doc)
        SUMMARY.lease("renewed" if result is not None else "lost")
        record_write(
            LEASES_COLLECTION,
            "success" if result is not None else "skipped",
            "renew",
            time.monotonic() - started,
        )
        return result

    async def release(self, name: str, owner: str) -> bool:
        """Release a lease only when the caller still owns it."""
        started = time.monotonic()
        result = await self._collection().delete_one({"name": name, "owner": owner})
        released = bool(result.deleted_count)
        SUMMARY.lease("released" if released else "not_owner")
        record_write(
            LEASES_COLLECTION,
            "success" if released else "skipped",
            "release",
            time.monotonic() - started,
        )
        return released

    async def get(self, name: str) -> dict[str, Any] | None:
        """Return a lease by name, or ``None`` when it does not exist."""
        return self._document(await self._collection().find_one({"name": name}))
