"""Repository for the `characterizations` collection (P3.2, petrosa_k8s#599).

Persists :class:`data_manager.models.characterization.Characterization`
documents in MongoDB and exposes lookup primitives:

  * :meth:`upsert` — write-or-replace by (strategy_id, strategy_version)
  * :meth:`get_latest` — most recent characterization for a strategy
  * :meth:`get_version` — exact (strategy_id, strategy_version) lookup

`upsert` uses the deterministic ``(strategy_id, strategy_version)`` pair
as the unique key; a re-run with the same inputs replaces the row in
place rather than appending a new one. Two strategies sharing a version
string is intentional: the version is the unit of reproducibility.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.characterization import Characterization

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

CHARACTERIZATIONS_COLLECTION = "characterizations"


class CharacterizationRepository(BaseRepository):
    """MongoDB-backed repository for characterization artifacts."""

    def __init__(
        self,
        mysql_adapter: MySQLAdapter | None,
        mongodb_adapter: MongoDBAdapter | None,
    ) -> None:
        super().__init__(mysql_adapter, mongodb_adapter)

    async def upsert(self, artifact: Characterization) -> bool:
        """Persist (or replace) a characterization keyed by (strategy_id, version).

        Returns True iff the write succeeded. Re-running with identical
        inputs replaces the document in place — reproducible runs do not
        bloat the collection. The caller is responsible for verifying
        that ``inputs_hash`` and ``metrics`` match across runs.
        """
        if not self.mongodb or not getattr(self.mongodb, "db", None):
            logger.warning("characterizations: mongodb not available; skipping write")
            return False

        artifact.validate_required_metrics()
        doc = artifact.model_dump()
        # Re-derive _id from the deterministic key so the unique index
        # exists without a separate ensure_indexes call.
        doc["_id"] = f"{artifact.strategy_id}::{artifact.strategy_version}"
        try:
            await self.mongodb.db[CHARACTERIZATIONS_COLLECTION].replace_one(
                {"_id": doc["_id"]},
                doc,
                upsert=True,
            )
            return True
        except Exception as exc:  # pragma: no cover — defensive
            logger.error(
                "characterizations: failed to persist artifact: %s", exc, exc_info=True
            )
            return False

    async def get_version(
        self, strategy_id: str, strategy_version: str
    ) -> Characterization | None:
        """Look up a characterization by exact (strategy_id, strategy_version)."""
        if not self.mongodb or not getattr(self.mongodb, "db", None):
            return None
        doc = await self.mongodb.db[CHARACTERIZATIONS_COLLECTION].find_one(
            {"strategy_id": strategy_id, "strategy_version": strategy_version}
        )
        return _document_to_artifact(doc)

    async def get_latest(self, strategy_id: str) -> Characterization | None:
        """Return the most recent characterization for a strategy."""
        if not self.mongodb or not getattr(self.mongodb, "db", None):
            return None
        cursor = (
            self.mongodb.db[CHARACTERIZATIONS_COLLECTION]
            .find({"strategy_id": strategy_id})
            .sort("created_at", -1)
            .limit(1)
        )
        docs = await cursor.to_list(length=1)
        return _document_to_artifact(docs[0]) if docs else None

    async def get_by_strategy_revision(
        self, strategy_id: str, strategy_revision_id: str
    ) -> Characterization | None:
        """Return the most recent characterization for a (strategy, revision) pair.

        FR53 / P3.4 (#179): consumers (CIO refusal path / FR20 evaluator
        binding) use this to confirm a characterization exists for the exact
        revision the live intent carries — None ⇒ stale, refuse.
        """
        if not self.mongodb or not getattr(self.mongodb, "db", None):
            return None
        cursor = (
            self.mongodb.db[CHARACTERIZATIONS_COLLECTION]
            .find(
                {
                    "strategy_id": strategy_id,
                    "strategy_revision_id": strategy_revision_id,
                }
            )
            .sort("created_at", -1)
            .limit(1)
        )
        docs = await cursor.to_list(length=1)
        return _document_to_artifact(docs[0]) if docs else None


def _document_to_artifact(doc: dict[str, Any] | None) -> Characterization | None:
    """Convert a stored Mongo document back to a `Characterization`.

    Drops the internal `_id` field; Pydantic does not own it.
    """
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return Characterization(**doc)
