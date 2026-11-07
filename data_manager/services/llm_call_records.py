"""LLM gateway call records: validation and the idempotent Mongo store (petrosa-data-manager#841).

The LLM gateway publishes one ``LlmCallRecord`` (``petrosa-contracts==0.2.0``) per provider attempt. A record
carries metadata and counts only, never a prompt or a response body (the contract forbids extra fields).

Storage:

- Mongo ``llm_call_records`` is the operational store. ``_id`` is ``"<request_id>:<attempt_no>"``, so a
  redelivered record is a duplicate-key no-op: each attempt is persisted once.
- Every new record is marked ``_mysql_copy_pending`` for the MySQL historic copy. The TTL
  (``LLM_CALL_RECORDS_TTL_SECONDS`` on ``started_at``) only applies once ``mysql_copied_at`` exists, as
  for ``execution_events``, so nothing expires before it is in the history.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from petrosa_contracts.llm_call_record import LlmCallRecord
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

COLLECTION = "llm_call_records"
#: Indexes for the usage queries (per caller, model alias, decision and day); the TTL index comes from the
#: persistence registry through ``MongoDBAdapter.ensure_indexes``.
USAGE_INDEXES: tuple[tuple[str, list[tuple[str, int]]], ...] = (
    ("started_at_1", [("started_at", ASCENDING)]),
    ("caller_1_started_at_1", [("caller", ASCENDING), ("started_at", ASCENDING)]),
    (
        "model_alias_1_started_at_1",
        [("model_alias", ASCENDING), ("started_at", ASCENDING)],
    ),
    ("decision_id_1", [("decision_id", ASCENDING)]),
    (
        "mysql_copy_pending_1",
        [("_mysql_copy_pending", ASCENDING), ("started_at", ASCENDING)],
    ),
)


class InvalidLlmCallRecord(ValueError):
    """The payload is not a valid ``LlmCallRecord`` (schema, an extra field, a naive timestamp...)."""


def parse_record(payload: bytes | str | dict[str, Any]) -> LlmCallRecord:
    """Validate one published payload against the pinned contract."""
    try:
        if isinstance(payload, dict):
            return LlmCallRecord.model_validate(payload)
        return LlmCallRecord.model_validate_json(payload)
    except Exception as exc:  # pydantic ValidationError, or bad JSON
        raise InvalidLlmCallRecord(str(exc)) from exc


def record_id(record: LlmCallRecord) -> str:
    return f"{record.request_id}:{record.attempt_no}"


def to_document(record: LlmCallRecord, received_at: datetime) -> dict[str, Any]:
    """The Mongo document: the record's fields, its id, and the bookkeeping for the MySQL copy."""
    document = record.model_dump(mode="python")
    document["outcome"] = record.outcome.value
    document["started_at"] = record.started_at.astimezone(UTC)
    document["_id"] = record_id(record)
    document["received_at"] = received_at
    document["_mysql_copy_pending"] = True
    return document


class LlmCallRecordStore:
    """Writes call records once (idempotent on ``request_id`` + ``attempt_no``)."""

    def __init__(self, mongodb_adapter: Any, clock: Any = None) -> None:
        self.mongodb = mongodb_adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._indexes_ready = False

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        await self.mongodb.ensure_indexes(COLLECTION)  # the registered TTL index
        collection = self.mongodb.db[COLLECTION]
        for name, keys in USAGE_INDEXES:
            await collection.create_index(keys, name=name)
        self._indexes_ready = True

    async def save(self, record: LlmCallRecord) -> bool:
        """Persist ``record``; True when written now, False when it was already stored (a redelivery).

        Raises on any other database error, so the consumer does not acknowledge the message.
        """
        await self.ensure_indexes()
        try:
            await self.mongodb.db[COLLECTION].insert_one(
                to_document(record, self._clock())
            )
        except DuplicateKeyError:
            logger.info("llm call record %s already stored", record_id(record))
            return False
        return True
