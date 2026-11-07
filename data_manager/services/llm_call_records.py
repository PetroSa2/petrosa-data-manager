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

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from petrosa_contracts.llm_call_record import LlmCallRecord
from pydantic import BaseModel
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


# --- the MySQL historic copy (dm#841 PR 2) -----------------------------------------------------

MYSQL_TABLE = "llm_call_records"
_ROW_FIELDS = (
    "request_id",
    "attempt_no",
    "caller",
    "priority_class",
    "model_alias",
    "provider_model",
    "decision_id",
    "investigation_id",
    "prompt_version",
    "started_at",
    "queue_wait_ms",
    "latency_ms",
    "outcome",
    "provider_status",
    "prompt_tokens",
    "completion_tokens",
    "received_at",
)


class LlmCallRecordRow(BaseModel):
    """One MySQL ``llm_call_records`` row (``record_key`` = ``request_id:attempt_no``)."""

    record_key: str
    request_id: str
    attempt_no: int
    caller: str
    priority_class: str
    model_alias: str
    provider_model: str | None = None
    decision_id: str | None = None
    investigation_id: str | None = None
    prompt_version: str | None = None
    started_at: datetime
    queue_wait_ms: float | None = None
    latency_ms: float
    outcome: str
    provider_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    received_at: datetime


def row_from_document(document: dict[str, Any]) -> LlmCallRecordRow:
    return LlmCallRecordRow(
        record_key=str(document["_id"]),
        **{field: document.get(field) for field in _ROW_FIELDS},
    )


async def copy_pending(
    mongodb_adapter: Any,
    mysql_adapter: Any,
    *,
    limit: int = 500,
    now: datetime | None = None,
) -> dict[str, int]:
    """Copy one batch of not-yet-copied records to MySQL, then mark them copied.

    Insert-only on ``record_key``: a record copied twice is a no-op in MySQL. ``mysql_copied_at`` is set
    only after the MySQL write returns, so the TTL never expires a record that is not in the history.
    Raises on a failed MySQL write; nothing is marked copied then.
    """
    collection = mongodb_adapter.db[COLLECTION]
    documents = await (
        collection.find({"_mysql_copy_pending": True})
        .sort("started_at", 1)
        .limit(limit)
        .to_list(length=limit)
    )
    if not documents:
        return {"copied": 0, "duplicates": 0}
    rows = [row_from_document(d) for d in documents]
    result = await asyncio.to_thread(
        mysql_adapter.write,
        rows,
        MYSQL_TABLE,
        insert_only=True,
        natural_key="record_key",
    )
    if getattr(result, "failed", 0):
        raise RuntimeError(
            f"llm_call_records MySQL copy failed for {result.failed} rows"
        )
    await collection.update_many(
        {"_id": {"$in": [d["_id"] for d in documents]}},
        {
            "$set": {
                "mysql_copied_at": now or datetime.now(UTC),
                "_mysql_copy_pending": False,
            }
        },
    )
    return {
        "copied": len(documents),
        "duplicates": int(getattr(result, "duplicates", 0) or 0),
    }


async def stuck_uncopied_count(
    mongodb_adapter: Any, older_than_seconds: float, now: datetime | None = None
) -> int:
    """Records still waiting for the MySQL copy after ``older_than_seconds``: a broken copy shows here."""
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=older_than_seconds)
    return int(
        await mongodb_adapter.db[COLLECTION].count_documents(
            {"_mysql_copy_pending": True, "received_at": {"$lt": cutoff}}
        )
    )


# --- usage (dm#841 PR 3) ---------------------------------------------------------------------

USAGE_GROUPS = ("caller", "model_alias", "decision_id", "day")
#: The usage query is bounded on the server and on the client: never longer than this.
USAGE_TIMEOUT_SECONDS = 2.0
MAX_USAGE_WINDOW = timedelta(days=31)


class UsageQueryTimeout(TimeoutError):
    """The usage aggregation did not finish within ``USAGE_TIMEOUT_SECONDS``."""


def usage_pipeline(
    group_by: str,
    start: datetime,
    end: datetime,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One server-side aggregation: counts, tokens, latency/wait percentiles and outcomes per group."""
    if group_by not in USAGE_GROUPS:
        raise ValueError(f"group_by must be one of {USAGE_GROUPS}")
    match: dict[str, Any] = {"started_at": {"$gte": start, "$lt": end}}
    for field, value in (filters or {}).items():
        if value:
            match[field] = value
    key: Any = (
        {
            "$dateToString": {
                "format": "%Y-%m-%d",
                "date": "$started_at",
                "timezone": "UTC",
            }
        }
        if group_by == "day"
        else f"${group_by}"
    )

    def outcome(name: str) -> dict[str, Any]:
        return {"$sum": {"$cond": [{"$eq": ["$outcome", name]}, 1, 0]}}

    return [
        {"$match": match},
        {
            "$group": {
                "_id": key,
                "attempts": {"$sum": 1},
                "requests": {"$sum": {"$cond": [{"$eq": ["$attempt_no", 1]}, 1, 0]}},
                "prompt_tokens": {"$sum": {"$ifNull": ["$prompt_tokens", 0]}},
                "completion_tokens": {"$sum": {"$ifNull": ["$completion_tokens", 0]}},
                "latency_ms": {
                    "$percentile": {
                        "input": "$latency_ms",
                        "p": [0.5, 0.95],
                        "method": "approximate",
                    }
                },
                "queue_wait_ms": {
                    "$percentile": {
                        "input": "$queue_wait_ms",
                        "p": [0.5, 0.95],
                        "method": "approximate",
                    }
                },
                "success": outcome("success"),
                "throttled": outcome("throttled"),
                "timeout": outcome("timeout"),
                "error": outcome("error"),
            }
        },
        {"$sort": {"attempts": -1}},
        {"$limit": 500},
    ]


def _group_row(group_by: str, doc: dict[str, Any]) -> dict[str, Any]:
    def pct(values: Any) -> dict[str, float | None]:
        values = values if isinstance(values, list) else []
        return {
            "p50": values[0] if len(values) > 0 else None,
            "p95": values[1] if len(values) > 1 else None,
        }

    return {
        group_by: doc.get("_id"),
        "attempts": doc.get("attempts", 0),
        "requests": doc.get("requests", 0),
        "tokens": {
            "prompt": doc.get("prompt_tokens", 0),
            "completion": doc.get("completion_tokens", 0),
        },
        "latency_ms": pct(doc.get("latency_ms")),
        "queue_wait_ms": pct(doc.get("queue_wait_ms")),
        "outcomes": {
            o: doc.get(o, 0) for o in ("success", "throttled", "timeout", "error")
        },
    }


async def usage(
    mongodb_adapter: Any,
    group_by: str,
    start: datetime,
    end: datetime,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """LLM usage per ``group_by`` over [start, end), bounded at ``USAGE_TIMEOUT_SECONDS``."""
    if end <= start or end - start > MAX_USAGE_WINDOW:
        raise ValueError("the window must be positive and at most 31 days")
    pipeline = usage_pipeline(group_by, start, end, filters)
    collection = mongodb_adapter.db[COLLECTION]
    try:
        async with asyncio.timeout(USAGE_TIMEOUT_SECONDS):
            cursor = collection.aggregate(
                pipeline, maxTimeMS=int(USAGE_TIMEOUT_SECONDS * 1000)
            )
            docs = await cursor.to_list(length=500)
    except TimeoutError as exc:
        raise UsageQueryTimeout(
            f"llm usage query exceeded {USAGE_TIMEOUT_SECONDS:.0f} s"
        ) from exc
    return {
        "group_by": group_by,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "filters": {k: v for k, v in (filters or {}).items() if v},
        "groups": [_group_row(group_by, d) for d in docs],
    }
