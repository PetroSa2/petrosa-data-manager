"""LLM gateway call records: the pinned contract and the idempotent store (petrosa-data-manager#841)."""

from __future__ import annotations

import importlib.metadata
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

from data_manager import persistence_registry as registry
from data_manager.services import llm_call_records as llm

NOW = datetime(2026, 11, 7, 20, 30, tzinfo=UTC)

#: A record as the LLM gateway (petrosa-llm-gateway#16) serialises it: model_dump_json() of LlmCallRecord.
GATEWAY_PAYLOAD = {
    "schema_version": "v1",
    "request_id": "req-7f3a",
    "attempt_no": 2,
    "caller": "petrosa-cio",
    "priority_class": "decision",
    "model_alias": "cio-strategist",
    "provider_model": "nvidia/llama-3.3-70b",
    "decision_id": "d-123",
    "investigation_id": None,
    "prompt_version": "v4",
    "started_at": "2026-11-07T20:29:58.120000Z",
    "queue_wait_ms": 41.5,
    "latency_ms": 1830.0,
    "outcome": "throttled",
    "provider_status": 429,
    "prompt_tokens": None,
    "completion_tokens": None,
}


def test_the_pinned_contract_version_is_installed():
    assert importlib.metadata.version("petrosa-contracts") == "0.2.0"


def test_a_gateway_payload_parses_from_bytes_and_dict():
    from_bytes = llm.parse_record(json.dumps(GATEWAY_PAYLOAD).encode())
    from_dict = llm.parse_record(GATEWAY_PAYLOAD)
    assert from_bytes == from_dict
    assert llm.record_id(from_bytes) == "req-7f3a:2"


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "the system prompt"},  # a body never rides along (extra="forbid")
        {"started_at": "2026-11-07T20:29:58"},  # naive timestamp
        {"attempt_no": 0},
        {"outcome": "maybe"},
    ],
)
def test_invalid_payloads_are_rejected(change):
    with pytest.raises(llm.InvalidLlmCallRecord) as rejected:
        llm.parse_record({**GATEWAY_PAYLOAD, **change})
    assert str(rejected.value)


def test_the_document_carries_the_id_utc_time_and_the_copy_marker():
    document = llm.to_document(llm.parse_record(GATEWAY_PAYLOAD), NOW)
    assert document["_id"] == "req-7f3a:2"
    assert document["started_at"].tzinfo is not None
    assert document["outcome"] == "throttled"
    assert document["_mysql_copy_pending"] is True
    assert document["received_at"] == NOW
    assert "prompt" not in document and "completion" not in document


def _store(insert_one):
    collection = MagicMock()
    collection.insert_one = insert_one
    collection.create_index = AsyncMock()
    adapter = MagicMock()
    adapter.db.__getitem__.return_value = collection
    adapter.ensure_indexes = AsyncMock()
    return llm.LlmCallRecordStore(adapter, clock=lambda: NOW), adapter, collection


@pytest.mark.asyncio
async def test_a_record_is_persisted_once_even_when_redelivered():
    stored: dict[str, dict] = {}

    async def insert_one(document):
        if document["_id"] in stored:
            raise DuplicateKeyError("E11000 duplicate key")
        stored[document["_id"]] = document

    store, adapter, collection = _store(insert_one)
    record = llm.parse_record(GATEWAY_PAYLOAD)

    assert await store.save(record) is True
    assert await store.save(record) is False  # the redelivery is a no-op
    assert list(stored) == ["req-7f3a:2"]
    adapter.ensure_indexes.assert_awaited_once_with("llm_call_records")  # TTL index
    assert collection.create_index.await_count == len(llm.USAGE_INDEXES)


@pytest.mark.asyncio
async def test_a_database_error_propagates_so_the_message_is_not_acknowledged():
    store, _, _ = _store(AsyncMock(side_effect=RuntimeError("socket timeout")))
    with pytest.raises(RuntimeError) as failed:
        await store.save(llm.parse_record(GATEWAY_PAYLOAD))
    assert "socket timeout" in str(failed.value)


def test_the_collection_is_registered_durable_with_a_ttl_after_the_mysql_copy():
    entry = registry.entry_for_collection("llm_call_records")
    assert entry is not None and entry.classification == "durable"
    assert entry.mysql_table == "llm_call_records"
    policy = registry.ttl_policy_for_collection("llm_call_records")
    assert policy.field == "started_at"
    assert policy.seconds == registry.LLM_CALL_RECORDS_TTL_SECONDS == 7 * 86400
    assert policy.partial_filter_expression == {"mysql_copied_at": {"$exists": True}}
