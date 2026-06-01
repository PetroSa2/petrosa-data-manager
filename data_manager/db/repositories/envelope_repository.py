"""Repository for the ``envelopes`` Mongo collection (#188, P4.6-AC2 / FR62).

Versioned reads + writes for active envelopes, per ``strategy_or_portfolio_key``.

  * :meth:`get_active_envelope` — newest version for a key (AC2.e).
  * :meth:`get_version` — exact (key, version-int) lookup.
  * :meth:`list_versions` — version numbers for a key, descending, for the
    operator history pane.
  * :meth:`insert_next_version` — write a new monotonically-versioned document
    for a given key (AC2.b). Concurrency-safe via Mongo's unique ``_id``
    constraint: two writers racing each other will each compute ``v<n>``,
    but only one insert succeeds; the loser retries with ``v<n+1>``.
  * :meth:`ensure_indexes` — create the compound
    ``(strategy_or_portfolio_key, version DESC)`` index (AC2.d). Idempotent.

Append-only by design (AC2.c): the repository exposes no update or delete
methods. Old versions remain queryable for breach-event audit hydration
in 692.6.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pymongo.errors import DuplicateKeyError

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.envelope import Envelope, EnvelopeSource

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter
    from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

ENVELOPES_COLLECTION = "envelopes"
"""Default collection name; overridable per-instance for tests."""

_MAX_INSERT_RETRIES = 8
"""Cap on racing-writer retries before surfacing the conflict as an error.

The retry loop computes ``v<n+1>`` after each conflict, so 8 iterations
covers 8 concurrent writers — well beyond realistic contention on the
envelope-write path (operator approvals are human-paced; characterization
is sequential per pipeline run).
"""


class EnvelopeRepository(BaseRepository):
    """MongoDB-backed repository for versioned envelopes (per FR62)."""

    def __init__(
        self,
        mysql_adapter: MySQLAdapter | None = None,
        mongodb_adapter: MongoDBAdapter | None = None,
        collection_name: str = ENVELOPES_COLLECTION,
    ) -> None:
        super().__init__(mysql_adapter=mysql_adapter, mongodb_adapter=mongodb_adapter)
        self._collection_name = collection_name

    def _collection(self):  # type: ignore[no-untyped-def]
        if self.mongodb is None:  # pragma: no cover — wiring guard
            raise RuntimeError("EnvelopeRepository requires a MongoDB adapter")
        return self.mongodb.db[self._collection_name]

    @staticmethod
    def _doc_to_envelope(doc: dict[str, Any] | None) -> Envelope | None:
        if doc is None:
            return None
        payload = {k: v for k, v in doc.items() if k != "_id"}
        return Envelope.model_validate(payload)

    async def ensure_indexes(self) -> None:
        """Create the compound ``(strategy_or_portfolio_key, version DESC)`` index (AC2.d).

        Idempotent — repeated calls are no-ops. Should be invoked once during
        service startup (e.g. from ``data_manager.startup.ensure_collection_indexes``).
        """
        col = self._collection()
        await col.create_index(
            [("strategy_or_portfolio_key", 1), ("version", -1)],
            name="strategy_or_portfolio_key_1_version_-1",
            background=True,
        )

    async def get_active_envelope(
        self, key: str
    ) -> tuple[int, dict[str, Any], EnvelopeSource] | None:
        """Return the latest envelope for ``key`` (AC2.e).

        Returns a 3-tuple ``(version, value, source)`` for the highest-version
        document with ``strategy_or_portfolio_key == key``, or ``None`` if no
        envelope exists for that key.

        Used by 692.3 consumers (decision-path envelope hydration) — the
        return shape is the minimal contract that path needs.
        """
        col = self._collection()
        doc = await col.find_one(
            {"strategy_or_portfolio_key": key},
            sort=[("version", -1)],
        )
        if doc is None:
            return None
        return doc["version"], doc["value"], doc["source"]

    async def get_full_active_envelope(self, key: str) -> Envelope | None:
        """Same as :meth:`get_active_envelope`, but returns the full Envelope model.

        For consumers (e.g. 692.6 breach-event audit hydration) that need
        ``signed_action_id`` / ``originating_characterization_revision`` /
        ``operator_id``.
        """
        col = self._collection()
        doc = await col.find_one(
            {"strategy_or_portfolio_key": key},
            sort=[("version", -1)],
        )
        return self._doc_to_envelope(doc)

    async def get_version(self, key: str, version: int) -> Envelope | None:
        """Exact (key, version) lookup for historical envelope queries."""
        col = self._collection()
        doc = await col.find_one({"strategy_or_portfolio_key": key, "version": version})
        return self._doc_to_envelope(doc)

    async def list_active_envelopes(
        self, *, key: str | None = None, limit: int = 200
    ) -> list[Envelope]:
        """Return the highest-version envelope per ``strategy_or_portfolio_key``.

        Powers the dashboard ``current[]`` pane (P4.6-AC4.a / #203). When
        ``key`` is supplied, the result is at most one element. When omitted,
        returns one row per distinct key in the store, capped at ``limit``.

        Implementation note: a simple ``$group``+``$first`` aggregation over
        a sort by ``version DESC`` per key. Mongo doesn't directly support
        "max-version per group" without a sort upstream — that's what the
        pipeline does.
        """
        col = self._collection()
        match: dict[str, Any] = {}
        if key:
            match["strategy_or_portfolio_key"] = key
        pipeline: list[dict[str, Any]] = []
        if match:
            pipeline.append({"$match": match})
        pipeline.extend(
            [
                {"$sort": {"strategy_or_portfolio_key": 1, "version": -1}},
                {
                    "$group": {
                        "_id": "$strategy_or_portfolio_key",
                        "doc": {"$first": "$ROOT"},
                    }
                },
                {"$replaceRoot": {"newRoot": "$doc"}},
                {"$sort": {"strategy_or_portfolio_key": 1}},
                {"$limit": max(1, min(int(limit), 200))},
            ]
        )
        out: list[Envelope] = []
        async for doc in col.aggregate(pipeline):
            env = self._doc_to_envelope(doc)
            if env is not None:
                out.append(env)
        return out

    async def list_versions(self, key: str, limit: int = 50) -> list[int]:
        """Version numbers for ``key`` in descending order (operator history pane).

        ``limit`` defaults to 50 to keep the history-pane payload small;
        callers needing the full history can pass a larger limit.
        """
        col = self._collection()
        cursor = (
            col.find(
                {"strategy_or_portfolio_key": key},
                projection={"version": 1, "_id": 0},
            )
            .sort("version", -1)
            .limit(limit)
        )
        return [doc["version"] async for doc in cursor]

    async def insert_next_version(self, envelope: Envelope) -> Envelope:
        """Insert ``envelope`` at version = (latest-for-key + 1) (AC2.b).

        The caller is responsible for setting every non-version, non-id
        field on the ``envelope`` argument; this method:

        * Computes the next ``version`` for ``envelope.strategy_or_portfolio_key``.
        * Stamps ``envelope.version`` and ``envelope.envelope_id``.
        * Inserts with the composite Mongo ``_id`` ``"<key>:v<n>"``.
        * Retries on ``DuplicateKeyError`` (concurrent writer raced us) by
          recomputing the next version, up to ``_MAX_INSERT_RETRIES``.

        Returns the version-stamped, persisted model. Raises ``RuntimeError``
        if the retry cap is exhausted (theoretically unreachable under
        realistic contention; surfaces as a hard failure rather than a
        silent slot collision).
        """
        col = self._collection()
        key = envelope.strategy_or_portfolio_key

        for attempt in range(_MAX_INSERT_RETRIES):
            latest = await col.find_one(
                {"strategy_or_portfolio_key": key},
                sort=[("version", -1)],
                projection={"version": 1, "_id": 0},
            )
            next_version = (latest["version"] + 1) if latest else 1

            stamped = envelope.model_copy(
                update={
                    "version": next_version,
                    "envelope_id": f"{key}:v{next_version}",
                }
            )
            document = stamped.model_dump(mode="json")
            document["_id"] = stamped.doc_id()
            try:
                await col.insert_one(document)
                logger.info(
                    "envelope.inserted key=%s version=%d source=%s signed_action_id=%s",
                    key,
                    next_version,
                    stamped.source,
                    stamped.signed_action_id,
                )
                return stamped
            except DuplicateKeyError:
                logger.warning(
                    "envelope.insert.race key=%s version=%d attempt=%d — retrying",
                    key,
                    next_version,
                    attempt + 1,
                )
                continue

        raise RuntimeError(
            f"envelope.insert.cap_exhausted key={key!r} after {_MAX_INSERT_RETRIES} attempts"
        )

    async def seed_legacy_characterization_envelopes(
        self,
        legacy_envelopes: list[tuple[str, dict[str, Any], str | None, str]],
    ) -> int:
        """One-shot migration helper (AC2.f).

        Seeds existing characterization-derived envelopes — passed by the
        caller as a list of ``(key, value, char_revision, signed_action_id)``
        tuples — as ``version=1`` documents with ``source='characterization'``.

        Skips any key that already has at least one document (idempotent,
        re-runnable). Returns the count of envelopes actually seeded.

        The caller is responsible for sourcing the legacy data — this
        method intentionally does not query upstream collections so it
        can be unit-tested without those dependencies, and so the data
        flow is auditable.
        """
        col = self._collection()
        seeded = 0
        for key, value, char_revision, signed_action_id in legacy_envelopes:
            existing = await col.find_one(
                {"strategy_or_portfolio_key": key},
                projection={"_id": 1},
            )
            if existing is not None:
                continue
            seed = Envelope(
                envelope_id=f"{key}:v1",
                version=1,
                strategy_or_portfolio_key=key,
                value=value,
                source="characterization",
                originating_characterization_revision=char_revision,
                operator_id=None,
                signed_action_id=signed_action_id,
            )
            document = seed.model_dump(mode="json")
            document["_id"] = seed.doc_id()
            try:
                await col.insert_one(document)
                seeded += 1
            except DuplicateKeyError:
                # Race with a live writer for the same key; skip and let
                # the live write win (it's strictly newer than this seed).
                logger.warning(
                    "envelope.seed.race key=%s — skipped (live writer won)",
                    key,
                )
                continue
        logger.info("envelope.seed_legacy.complete seeded=%d", seeded)
        return seeded
