"""Repository for the ``pending_envelope_changes`` Mongo collection (#187, P4.6-AC1 / FR62).

Reads + status-transitions for envelope-change proposals awaiting
operator approval. Pairs with :mod:`data_manager.db.repositories.envelope_repository`
(which stores the *accepted* versioned envelopes) and with
:mod:`data_manager.api.routes.envelopes` (which exposes the operator
endpoints).

Append-only with respect to ``change_id``: status moves are in-place
updates that gate on ``status == 'pending'``, so a second concurrent
resolution attempt fails fast rather than silently overwriting the
first one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.envelope_change import (
    EnvelopeChangeResolution,
    PendingEnvelopeChange,
)

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

PENDING_ENVELOPE_CHANGES_COLLECTION = "pending_envelope_changes"
"""Default collection name; overridable per-instance for tests."""


class PendingChangeNotFoundError(LookupError):
    """Raised when the operator references a ``change_id`` that does not exist."""


class PendingChangeAlreadyResolvedError(RuntimeError):
    """Raised when the operator tries to resolve a change that's already non-pending.

    Carries the existing ``status`` and ``resolution`` so the route can
    return a useful 409 response.
    """

    def __init__(self, change_id: str, status: str) -> None:
        super().__init__(
            f"pending envelope change {change_id!r} already resolved (status={status})"
        )
        self.change_id = change_id
        self.status = status


class PendingEnvelopeChangeRepository(BaseRepository):
    """MongoDB-backed repository for pending envelope-change proposals (FR62)."""

    def __init__(
        self,
        mysql_adapter: MySQLAdapter | None = None,
        mongodb_adapter: MongoDBAdapter | None = None,
        collection_name: str = PENDING_ENVELOPE_CHANGES_COLLECTION,
    ) -> None:
        super().__init__(mysql_adapter=mysql_adapter, mongodb_adapter=mongodb_adapter)
        self._collection_name = collection_name

    def _collection(self):  # type: ignore[no-untyped-def]
        if self.mongodb is None:  # pragma: no cover — wiring guard
            raise RuntimeError(
                "PendingEnvelopeChangeRepository requires a MongoDB adapter"
            )
        return self.mongodb.db[self._collection_name]

    @staticmethod
    def _doc_to_model(doc: dict[str, Any] | None) -> PendingEnvelopeChange | None:
        if doc is None:
            return None
        payload = {k: v for k, v in doc.items() if k != "_id"}
        return PendingEnvelopeChange.model_validate(payload)

    async def ensure_indexes(self) -> None:
        """Create the indexes that back the operator-facing queries.

        Idempotent. Invoked once during service startup from
        :mod:`data_manager.leader_election`.
        """
        col = self._collection()
        await col.create_index(
            [("status", 1), ("created_at", -1)],
            name="status_1_created_at_-1",
            background=True,
        )
        await col.create_index(
            [("strategy_or_portfolio_key", 1), ("status", 1)],
            name="strategy_or_portfolio_key_1_status_1",
            background=True,
        )

    async def list_pending(self, limit: int = 200) -> list[PendingEnvelopeChange]:
        """Return all pending changes (AC1.a) in newest-first order.

        ``limit`` caps the response size; the operator UI page is bounded
        and the worst case (every strategy/portfolio key pending at once)
        is still in the low hundreds — the cap prevents accidental
        unbounded scans rather than gating routine reads.
        """
        col = self._collection()
        cursor = col.find({"status": "pending"}).sort("created_at", -1).limit(limit)
        results: list[PendingEnvelopeChange] = []
        async for doc in cursor:
            change = self._doc_to_model(doc)
            if change is not None:
                results.append(change)
        return results

    async def get(self, change_id: str) -> PendingEnvelopeChange | None:
        """Look up one change by id. Returns ``None`` if not found."""
        col = self._collection()
        doc = await col.find_one({"_id": change_id})
        return self._doc_to_model(doc)

    async def insert(self, change: PendingEnvelopeChange) -> PendingEnvelopeChange:
        """Persist a new pending change.

        Provided for producers (and tests). Not used by the operator-facing
        route handlers in #187 — those only read + resolve.
        """
        col = self._collection()
        doc = change.model_dump(mode="json")
        doc["_id"] = change.change_id
        await col.insert_one(doc)
        logger.info(
            "pending_envelope_change.inserted change_id=%s key=%s char_rev=%s",
            change.change_id,
            change.strategy_or_portfolio_key,
            change.originating_characterization_revision,
        )
        return change

    async def resolve(
        self,
        change_id: str,
        *,
        target_status: str,
        resolution: EnvelopeChangeResolution,
    ) -> PendingEnvelopeChange:
        """Transition a pending change to ``accepted`` or ``rejected``.

        The update is gated on ``status == 'pending'`` so a concurrent
        second-resolver gets a deterministic conflict (``PendingChangeAlreadyResolvedError``)
        instead of silently overwriting the first resolver's outcome.

        Raises:
            PendingChangeNotFoundError: no document with ``change_id``.
            PendingChangeAlreadyResolvedError: the change is not pending.
        """
        if target_status not in ("accepted", "rejected"):
            raise ValueError(
                f"target_status must be 'accepted' or 'rejected', not {target_status!r}"
            )

        col = self._collection()
        # Conditional update — only flip status if still pending.
        update_result = await col.update_one(
            {"_id": change_id, "status": "pending"},
            {
                "$set": {
                    "status": target_status,
                    "resolution": resolution.model_dump(mode="json"),
                }
            },
        )

        if update_result.matched_count == 1:
            doc = await col.find_one({"_id": change_id})
            model = self._doc_to_model(doc)
            assert model is not None  # noqa: S101 — just-updated doc must exist
            logger.info(
                "pending_envelope_change.resolved change_id=%s status=%s operator=%s",
                change_id,
                target_status,
                resolution.operator_id,
            )
            return model

        # No match — either missing or already resolved. Disambiguate.
        existing = await col.find_one({"_id": change_id}, projection={"status": 1})
        if existing is None:
            raise PendingChangeNotFoundError(change_id)
        raise PendingChangeAlreadyResolvedError(
            change_id=change_id, status=existing.get("status", "unknown")
        )
