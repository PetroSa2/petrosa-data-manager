"""Repository for the `strategy_registry` Mongo collection (#195, FR54).

CRUD-light (no update, no delete) layer over operator-submitted strategy
definitions. Each document is a verbatim :class:`RegisteredStrategy` row.

Methods:

* :meth:`insert` — persist a candidate strategy. Raises
  :class:`StrategyAlreadyRegisteredError` (HTTP 409 equivalent) on duplicate
  `strategy_id` via the Mongo unique-id constraint.
* :meth:`get` — exact `strategy_id` lookup.
* :meth:`list` — paged + status-filtered list, sorted newest-first.
* :meth:`ensure_indexes` — unique on `strategy_id` (enforced via `_id`) +
  compound `(status, registered_at DESC)` for the list endpoint.

This module deliberately exposes no update/delete: status transitions
(`candidate` → `accepted`/`rejected`) are a follow-up concern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.registered_strategy import (
    RegisteredStrategy,
    StrategyStatus,
)

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

STRATEGY_REGISTRY_COLLECTION = "strategy_registry"


class StrategyAlreadyRegisteredError(Exception):
    """Raised when an `insert` collides with an existing `strategy_id`.

    Translates to HTTP 409 in the route layer.
    """

    def __init__(self, strategy_id: str) -> None:
        super().__init__(f"strategy_id={strategy_id!r} already registered")
        self.strategy_id = strategy_id


class StrategyRegistryRepository(BaseRepository):
    """MongoDB-backed repository for the strategy registry (FR54)."""

    def __init__(
        self,
        mysql_adapter: MySQLAdapter | None = None,
        mongodb_adapter: MongoDBAdapter | None = None,
        collection_name: str = STRATEGY_REGISTRY_COLLECTION,
    ) -> None:
        super().__init__(mysql_adapter=mysql_adapter, mongodb_adapter=mongodb_adapter)
        self._collection_name = collection_name

    def _collection(self):  # type: ignore[no-untyped-def]
        if self.mongodb is None:  # pragma: no cover — wiring guard
            raise RuntimeError("StrategyRegistryRepository requires a MongoDB adapter")
        return self.mongodb.db[self._collection_name]

    @staticmethod
    def _doc_to_strategy(doc: dict[str, Any] | None) -> RegisteredStrategy | None:
        if doc is None:
            return None
        payload = {k: v for k, v in doc.items() if k != "_id"}
        return RegisteredStrategy.model_validate(payload)

    async def ensure_indexes(self) -> None:
        """Create indexes for the list endpoint (idempotent).

        The unique-on-`strategy_id` constraint is enforced via Mongo's
        built-in `_id` uniqueness — `insert` sets `_id = strategy_id`. The
        compound `(status, registered_at DESC)` index accelerates the
        list endpoint's status filter + reverse-chronological sort.
        """
        col = self._collection()
        await col.create_index(
            [("status", 1), ("registered_at", -1)],
            name="status_1_registered_at_-1",
            background=True,
        )

    async def insert(self, strategy: RegisteredStrategy) -> RegisteredStrategy:
        """Persist `strategy` as a new candidate.

        Raises :class:`StrategyAlreadyRegisteredError` if `strategy.strategy_id`
        already exists in the registry.
        """
        col = self._collection()
        doc = strategy.model_dump(mode="json")
        doc["_id"] = strategy.strategy_id
        try:
            await col.insert_one(doc)
        except DuplicateKeyError as exc:
            raise StrategyAlreadyRegisteredError(strategy.strategy_id) from exc
        logger.info(
            "strategy_registry.insert strategy_id=%s submitted_by=%s",
            strategy.strategy_id,
            strategy.submitted_by,
        )
        return strategy

    async def get(self, strategy_id: str) -> RegisteredStrategy | None:
        col = self._collection()
        doc = await col.find_one({"_id": strategy_id})
        return self._doc_to_strategy(doc)

    async def list(
        self,
        *,
        status: StrategyStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RegisteredStrategy]:
        """List registered strategies newest-first.

        Filters by `status` when provided. `offset` + `limit` provide cursor-
        free pagination — fine for the registry's expected size (operators,
        not events).
        """
        col = self._collection()
        query: dict[str, Any] = {}
        if status is not None:
            query["status"] = status
        cursor = col.find(query).sort("registered_at", -1)
        # Tests' in-memory fake doesn't support `skip` on the cursor; collect
        # then slice. Real Mongo gives us `.skip(offset).limit(limit)` but
        # for the registry's size that's negligible.
        docs: list[dict[str, Any]] = []
        async for doc in cursor:
            docs.append(doc)
        sliced = docs[offset : offset + limit]
        results: list[RegisteredStrategy] = []
        for doc in sliced:
            parsed = self._doc_to_strategy(doc)
            if parsed is not None:
                results.append(parsed)
        return results
