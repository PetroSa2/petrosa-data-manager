"""Repository for the ``drawdown_breaches`` Mongo collection (P4.6-AC6 / #194).

One row per breach event emitted by tradeengine on the
``alerts.drawdown.breach.{strategy_id}`` NATS subject. AC6.b adds the
nullable ``envelope_version`` + ``envelope_source`` columns alongside
the legacy core fields. AC6.d / AC6.e — the collection accepts both
old (envelope_version=null) and new shapes; reads round-trip both.

Append-only — no update / delete API surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.drawdown_breach import DrawdownBreach

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

DRAWDOWN_BREACHES_COLLECTION = "drawdown_breaches"
"""Default collection name; overridable per-instance for tests."""


class DrawdownBreachRepository(BaseRepository):
    """Reads / writes for the ``drawdown_breaches`` collection."""

    def __init__(
        self,
        mongodb_adapter: MongoDBAdapter,
        mysql_adapter: MySQLAdapter | None = None,
        collection_name: str = DRAWDOWN_BREACHES_COLLECTION,
    ) -> None:
        super().__init__(mongodb_adapter=mongodb_adapter, mysql_adapter=mysql_adapter)
        self._collection_name = collection_name

    def _collection(self):  # type: ignore[no-untyped-def]
        return self.mongodb_adapter.get_collection(self._collection_name)

    @staticmethod
    def _doc_to_breach(doc: dict[str, Any] | None) -> DrawdownBreach | None:
        if doc is None:
            return None
        payload = dict(doc)
        # Map Mongo _id back to breach_id for the model.
        if "breach_id" not in payload and "_id" in payload:
            payload["breach_id"] = payload["_id"]
        payload.pop("_id", None)
        return DrawdownBreach.model_validate(payload)

    async def ensure_indexes(self) -> None:
        """Create indexes used by the dashboard + read API.

        * ``(strategy_id, detected_at DESC)`` — strategy breach history pane.
        * ``(envelope_version)`` — hydration lookups joining back to the
          ``envelopes`` collection.
        """
        col = self._collection()
        await col.create_index(
            [("strategy_id", 1), ("detected_at", -1)],
            name="strategy_detected_idx",
        )
        await col.create_index(
            [("envelope_version", 1)],
            name="envelope_version_idx",
            sparse=True,
        )

    async def insert(self, breach: DrawdownBreach) -> bool:
        """Insert one breach row idempotently (gated on Mongo ``_id``).

        Returns ``True`` on insert, ``False`` when a row with the same
        ``breach_id`` already exists (subscriber replay safe).
        """
        doc = breach.model_dump(mode="json")
        doc["_id"] = doc.pop("breach_id")
        col = self._collection()
        try:
            await col.insert_one(doc)
            return True
        except DuplicateKeyError:
            logger.info(
                "drawdown_breach_insert_dup",
                extra={"breach_id": breach.breach_id},
            )
            return False

    async def get_by_id(self, breach_id: str) -> DrawdownBreach | None:
        """Exact lookup by ``breach_id`` — backs ``GET /api/breaches/{id}``."""
        col = self._collection()
        doc = await col.find_one({"_id": breach_id})
        return self._doc_to_breach(doc)
