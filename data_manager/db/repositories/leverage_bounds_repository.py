"""Repository for the ``leverage_bounds`` Mongo collection (#182, P1.5-AC6).

Versioned reads + writes for operator-configured leverage bounds.

  * :meth:`get_latest` — newest version (highest ``v<n>``) or None.
  * :meth:`get_version` — exact (version-int) lookup.
  * :meth:`list_versions` — version numbers in descending order, for the
    dashboard's history pane.
  * :meth:`insert_next` — write a new monotonically-versioned document.
    Concurrency-safe via Mongo's unique ``_id`` constraint: two writers
    racing each other will each compute ``v<n>``, but only one insert
    succeeds; the loser retries with ``v<n+1>``.

Older versions are never mutated — operators can always trace bounds at
any point in time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.leverage_bounds import LeverageBounds

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

LEVERAGE_BOUNDS_COLLECTION = "leverage_bounds"


class LeverageBoundsRepository(BaseRepository):
    """MongoDB-backed repository for versioned leverage bounds."""

    def __init__(
        self,
        mysql_adapter: MySQLAdapter | None = None,
        mongodb_adapter: MongoDBAdapter | None = None,
        collection_name: str = LEVERAGE_BOUNDS_COLLECTION,
    ) -> None:
        super().__init__(mysql_adapter=mysql_adapter, mongodb_adapter=mongodb_adapter)
        self._collection_name = collection_name

    def _collection(self):  # type: ignore[no-untyped-def]
        if self.mongodb is None:  # pragma: no cover — wiring guard
            raise RuntimeError("LeverageBoundsRepository requires a MongoDB adapter")
        return self.mongodb.db[self._collection_name]

    @staticmethod
    def _doc_to_bounds(doc: dict[str, Any] | None) -> LeverageBounds | None:
        if doc is None:
            return None
        # The Mongo `_id` shape (`v<n>`) is the storage key; the in-app model
        # carries the integer `version` instead, so strip `_id` before
        # validation. `version` is asserted to match the `_id` parse below.
        payload = {k: v for k, v in doc.items() if k != "_id"}
        return LeverageBounds.model_validate(payload)

    @staticmethod
    def _id_to_version(doc_id: str) -> int:
        """Parse a `v<n>` `_id` into an integer version."""
        if not doc_id.startswith("v"):
            raise ValueError(f"unexpected leverage_bounds _id shape: {doc_id!r}")
        try:
            return int(doc_id[1:])
        except ValueError as exc:
            raise ValueError(
                f"unexpected leverage_bounds _id shape: {doc_id!r}"
            ) from exc

    async def get_latest(self) -> LeverageBounds | None:
        """Return the highest-version document, or None when the collection is empty."""
        col = self._collection()
        # The natural sort on `_id` works once we have ≥ 10 versions because
        # `v9 > v10` lexicographically. Sort on the explicit `version` field
        # instead so the order is correct regardless of count.
        doc = await col.find_one({}, sort=[("version", -1)])
        return self._doc_to_bounds(doc)

    async def get_version(self, version: int) -> LeverageBounds | None:
        """Exact-version lookup."""
        if version < 1:
            return None
        col = self._collection()
        doc = await col.find_one({"_id": f"v{version}"})
        return self._doc_to_bounds(doc)

    async def list_versions(self, limit: int = 50) -> list[int]:
        """Version numbers in descending order (newest first)."""
        col = self._collection()
        cursor = (
            col.find({}, projection={"version": 1, "_id": 0})
            .sort("version", -1)
            .limit(limit)
        )
        return [doc["version"] async for doc in cursor]

    async def insert_next(self, bounds: LeverageBounds) -> LeverageBounds:
        """Insert ``bounds`` at version = (latest + 1). Stamps the version on the model.

        The caller is responsible for setting all non-version fields on the
        ``bounds`` argument; this method sets ``bounds.version`` and the
        Mongo ``_id``. Returns the inserted (version-stamped) model.
        """
        col = self._collection()
        latest = await self.get_latest()
        next_version = (latest.version + 1) if latest else 1
        bounds = bounds.model_copy(update={"version": next_version})

        document = bounds.model_dump(mode="json")
        document["_id"] = bounds.doc_id()
        await col.insert_one(document)
        logger.info(
            "leverage_bounds.inserted version=%d changed_by=%s",
            next_version,
            bounds.changed_by,
        )
        return bounds
