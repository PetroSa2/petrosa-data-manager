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


# --- PR 2: the MySQL historic copy ---------------------------------------------------------------


def test_the_mysql_table_holds_every_row_field_keyed_by_record_key():
    from data_manager.db.mysql_adapter import MySQLAdapter

    adapter = MySQLAdapter("sqlite:///:memory:", role="serving")
    adapter._create_tables()
    table = adapter.tables["llm_call_records"]
    assert [c.name for c in table.primary_key.columns] == ["record_key"]
    assert set(llm.LlmCallRecordRow.model_fields) <= {c.name for c in table.columns}
    assert "prompt" not in table.c and "completion" not in table.c


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a):
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length):
        return list(self.docs)


def _copy_setup(pending, write_result):
    collection = MagicMock()
    collection.find = MagicMock(return_value=_Cursor(pending))
    collection.update_many = AsyncMock()
    collection.count_documents = AsyncMock(return_value=3)
    mongodb = MagicMock()
    mongodb.db.__getitem__.return_value = collection
    mysql = MagicMock()
    mysql.write = MagicMock(return_value=write_result)
    return mongodb, mysql, collection


@pytest.mark.asyncio
async def test_copy_writes_insert_only_then_marks_copied():
    document = llm.to_document(llm.parse_record(GATEWAY_PAYLOAD), NOW)
    mongodb, mysql, collection = _copy_setup(
        [document], MagicMock(failed=0, duplicates=0)
    )

    result = await llm.copy_pending(mongodb, mysql, limit=10, now=NOW)

    assert result == {"copied": 1, "duplicates": 0}
    rows, table = mysql.write.call_args.args
    assert table == "llm_call_records" and rows[0].record_key == "req-7f3a:2"
    assert mysql.write.call_args.kwargs == {
        "insert_only": True,
        "natural_key": "record_key",
    }
    query, update = collection.update_many.await_args.args
    assert query == {"_id": {"$in": ["req-7f3a:2"]}}
    assert update["$set"] == {"mysql_copied_at": NOW, "_mysql_copy_pending": False}


@pytest.mark.asyncio
async def test_a_failed_mysql_write_marks_nothing_copied():
    document = llm.to_document(llm.parse_record(GATEWAY_PAYLOAD), NOW)
    mongodb, mysql, collection = _copy_setup(
        [document], MagicMock(failed=1, duplicates=0)
    )
    with pytest.raises(RuntimeError, match="copy failed"):
        await llm.copy_pending(mongodb, mysql, now=NOW)
    collection.update_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_pending_writes_nothing():
    mongodb, mysql, collection = _copy_setup([], None)
    assert await llm.copy_pending(mongodb, mysql) == {"copied": 0, "duplicates": 0}
    mysql.write.assert_not_called()


@pytest.mark.asyncio
async def test_records_stuck_uncopied_are_counted_and_warned(caplog):
    from data_manager.maintenance import llm_call_records_copy as job

    mongodb, mysql, collection = _copy_setup([], None)
    manager = MagicMock(mongodb_adapter=mongodb, mysql_adapter=mysql)
    with caplog.at_level("WARNING"):
        result = await job.LlmCallRecordsCopy(manager).run_once()
    assert result["stale"] == 3
    assert "LLM_CALL_RECORDS_COPY_STUCK" in caplog.text
    query = collection.count_documents.await_args.args[0]
    assert query["_mysql_copy_pending"] is True and "$lt" in query["received_at"]
    assert job._stale["value"] == 3


def test_the_registry_entry_is_no_longer_pending():
    assert registry.entry_for_collection("llm_call_records").pending is False


@pytest.mark.asyncio
async def test_the_copy_loop_runs_only_as_leader_and_counts_failures(monkeypatch):
    from data_manager.maintenance import llm_call_records_copy as job

    monkeypatch.setattr(job, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(job, "COPY_INTERVAL_SECONDS", 0)
    added = []
    monkeypatch.setattr(job, "COPY_TOTAL", MagicMock(add=lambda v, a: added.append(a)))

    leader = MagicMock(is_leader=False)
    copier = job.LlmCallRecordsCopy(MagicMock(), leader_election=leader)
    calls = []

    async def run_once():
        calls.append(leader.is_leader)
        if len(calls) == 1:
            raise RuntimeError("mysql down")
        copier.running = False
        return {}

    monkeypatch.setattr(copier, "run_once", run_once)
    ticks = {"n": 0}
    real_sleep = __import__("asyncio").sleep

    async def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] == 2:
            leader.is_leader = True  # becomes leader after one idle cycle
        await real_sleep(0)

    monkeypatch.setattr(job.asyncio, "sleep", fake_sleep)
    await copier.start()

    assert calls == [True, True]  # never ran while not the leader
    assert {"result": "failure"} in added  # the failed batch is counted


def test_without_leader_election_the_single_pod_copies():
    from data_manager.maintenance import llm_call_records_copy as job

    assert job.LlmCallRecordsCopy(MagicMock())._is_leader() is True


def test_the_stale_gauge_reports_only_after_the_first_measurement(monkeypatch):
    from data_manager.maintenance import llm_call_records_copy as job

    monkeypatch.setitem(job._stale, "value", None)
    assert list(job._observe_stale(None)) == []
    monkeypatch.setitem(job._stale, "value", 4)
    assert [o.value for o in job._observe_stale(None)] == [4]


# --- PR 3: usage endpoints -------------------------------------------------------------------


def test_the_usage_pipeline_groups_and_computes_percentiles_server_side():
    start, end = NOW.replace(hour=0), NOW
    pipeline = llm.usage_pipeline(
        "caller", start, end, {"model_alias": "cio-strategist", "caller": None}
    )
    match, group = pipeline[0]["$match"], pipeline[1]["$group"]
    assert match == {
        "started_at": {"$gte": start, "$lt": end},
        "model_alias": "cio-strategist",
    }
    assert group["_id"] == "$caller"
    assert group["latency_ms"]["$percentile"]["p"] == [0.5, 0.95]
    assert group["queue_wait_ms"]["$percentile"]["method"] == "approximate"
    assert set(group) >= {
        "attempts",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "success",
        "throttled",
        "timeout",
        "error",
    }
    day = llm.usage_pipeline("day", start, end)[1]["$group"]["_id"]
    assert day["$dateToString"]["format"] == "%Y-%m-%d"
    with pytest.raises(ValueError) as bad:
        llm.usage_pipeline("prompt", start, end)
    assert "group_by" in str(bad.value)


class _AggCursor:
    def __init__(self, docs, delay=0.0):
        self.docs, self.delay = docs, delay

    async def to_list(self, length):
        import asyncio

        await asyncio.sleep(self.delay)
        return self.docs


def _usage_adapter(docs, delay=0.0):
    collection = MagicMock()
    collection.aggregate = MagicMock(return_value=_AggCursor(docs, delay))
    adapter = MagicMock()
    adapter.db.__getitem__.return_value = collection
    return adapter, collection


@pytest.mark.asyncio
async def test_usage_formats_each_group_with_tokens_percentiles_and_outcomes():
    doc = {
        "_id": "petrosa-cio",
        "attempts": 12,
        "requests": 10,
        "prompt_tokens": 9000,
        "completion_tokens": 1200,
        "latency_ms": [820.0, 2400.0],
        "queue_wait_ms": [15.0, 300.0],
        "success": 9,
        "throttled": 2,
        "timeout": 1,
        "error": 0,
    }
    adapter, collection = _usage_adapter([doc])
    body = await llm.usage(adapter, "caller", NOW.replace(hour=0), NOW)
    assert body["groups"] == [
        {
            "caller": "petrosa-cio",
            "attempts": 12,
            "requests": 10,
            "tokens": {"prompt": 9000, "completion": 1200},
            "latency_ms": {"p50": 820.0, "p95": 2400.0},
            "queue_wait_ms": {"p50": 15.0, "p95": 300.0},
            "outcomes": {"success": 9, "throttled": 2, "timeout": 1, "error": 0},
        }
    ]
    assert collection.aggregate.call_args.kwargs["maxTimeMS"] == 2000


@pytest.mark.asyncio
async def test_a_slow_usage_query_is_a_timeout_never_a_partial_answer(monkeypatch):
    monkeypatch.setattr(llm, "USAGE_TIMEOUT_SECONDS", 0.05)
    adapter, _ = _usage_adapter([], delay=1.0)
    with pytest.raises(llm.UsageQueryTimeout) as slow:
        await llm.usage(adapter, "day", NOW.replace(hour=0), NOW)
    assert "exceeded" in str(slow.value)


def test_the_usage_endpoint_answers_validates_and_bounds(monkeypatch):
    from fastapi.testclient import TestClient

    import data_manager.api.app as api_module

    adapter, _ = _usage_adapter([{"_id": "2026-11-07", "attempts": 1, "requests": 1}])
    monkeypatch.setattr(api_module, "db_manager", MagicMock(mongodb_adapter=adapter))
    client = TestClient(api_module.create_app())

    ok = client.get(
        "/api/v1/llm/usage",
        params={
            "group_by": "day",
            "from": "2026-11-07T00:00:00Z",
            "to": "2026-11-07T12:00:00Z",
        },
    )
    assert ok.status_code == 200 and ok.json()["groups"][0]["day"] == "2026-11-07"
    assert (
        client.get("/api/v1/llm/usage", params={"group_by": "prompt"}).status_code
        == 422
    )
    too_long = client.get(
        "/api/v1/llm/usage",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-11-07T00:00:00Z"},
    )
    assert too_long.status_code == 400

    async def slow(*_a, **_k):
        raise llm.UsageQueryTimeout("llm usage query exceeded 2 s")

    monkeypatch.setattr(llm, "usage", slow)
    timed_out = client.get("/api/v1/llm/usage")
    assert timed_out.status_code == 503
    assert timed_out.json()["detail"]["error"] == "usage_query_timeout"


# --- PR 4: the JetStream consumer ------------------------------------------------------------


class _Msg:
    def __init__(self, data, delivered=1):
        self.data = data
        self.metadata = MagicMock(num_delivered=delivered)
        self.acked = self.termed = False
        self.nak_delay = None

    async def ack(self):
        self.acked = True

    async def term(self):
        self.termed = True

    async def nak(self, delay=None):
        self.nak_delay = delay


def _consumer(save):
    from data_manager.consumer.llm_call_record_consumer import LlmCallRecordConsumer

    store = MagicMock()
    store.save = save
    return LlmCallRecordConsumer(MagicMock(), nats_client=MagicMock(), store=store)


@pytest.mark.asyncio
async def test_a_stored_record_is_acked_only_after_the_write():
    order = []

    async def save(record):
        order.append("save")
        return True

    consumer = _consumer(save)
    msg = _Msg(json.dumps(GATEWAY_PAYLOAD).encode())
    original_ack = msg.ack

    async def ack():
        order.append("ack")
        await original_ack()

    msg.ack = ack
    assert await consumer.handle(msg) == "stored"
    assert order == ["save", "ack"]


@pytest.mark.asyncio
async def test_a_redelivered_record_is_acked_without_a_second_write():
    consumer = _consumer(AsyncMock(return_value=False))
    msg = _Msg(json.dumps(GATEWAY_PAYLOAD).encode(), delivered=2)
    assert await consumer.handle(msg) == "duplicate"
    assert msg.acked is True


@pytest.mark.asyncio
async def test_a_database_error_naks_with_backoff_and_never_acks():
    from data_manager.consumer import llm_call_record_consumer as mod

    consumer = _consumer(AsyncMock(side_effect=RuntimeError("socket timeout")))
    msg = _Msg(json.dumps(GATEWAY_PAYLOAD).encode(), delivered=3)
    assert await consumer.handle(msg) == "retry"
    assert msg.acked is False and msg.nak_delay == mod.nak_delay(3) == 8.0
    assert mod.nak_delay(20) == mod.NAK_MAX_DELAY_SECONDS


@pytest.mark.asyncio
async def test_the_last_delivery_failing_is_warned_counted_and_terminated(caplog):
    from data_manager.consumer import llm_call_record_consumer as mod

    consumer = _consumer(AsyncMock(side_effect=RuntimeError("socket timeout")))
    msg = _Msg(json.dumps(GATEWAY_PAYLOAD).encode(), delivered=mod.MAX_DELIVER)
    with caplog.at_level("WARNING"):
        assert await consumer.handle(msg) == "exhausted"
    assert "LLM_CALL_RECORD_DELIVERY_EXHAUSTED" in caplog.text
    assert msg.termed is True and msg.acked is False


@pytest.mark.asyncio
async def test_an_invalid_payload_is_terminated_not_redelivered():
    save = AsyncMock()
    consumer = _consumer(save)
    msg = _Msg(json.dumps({**GATEWAY_PAYLOAD, "prompt": "body"}).encode())
    assert await consumer.handle(msg) == "invalid"
    assert msg.termed is True
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_stream_is_checked_never_created_and_one_durable_is_shared():
    from data_manager.consumer import llm_call_record_consumer as mod

    js = MagicMock()
    js.stream_info = AsyncMock(
        side_effect=[Exception("stream not found"), {"config": {}}]
    )
    js.add_stream = AsyncMock()
    js.pull_subscribe = AsyncMock(return_value="sub")
    nats_client = MagicMock()
    nats_client.nc.jetstream.return_value = js
    consumer = mod.LlmCallRecordConsumer(
        MagicMock(), nats_client=nats_client, store=MagicMock()
    )

    assert await consumer._bind() is False  # missing: ERROR, no subscription
    assert await consumer._bind() is True
    js.add_stream.assert_not_called()  # never created by data-manager
    args, kwargs = js.pull_subscribe.await_args
    assert args == (mod.SUBJECT,)
    assert kwargs["durable"] == mod.DURABLE and kwargs["stream"] == mod.STREAM
    config = kwargs["config"]
    assert config.ack_wait == 30 and config.max_deliver == 10
    assert str(config.ack_policy.value) == "explicit"


@pytest.mark.asyncio
async def test_the_run_loop_rechecks_the_stream_fetches_and_survives_timeouts(
    monkeypatch,
):
    import asyncio

    import nats.errors

    from data_manager.consumer import llm_call_record_consumer as mod

    consumer = mod.LlmCallRecordConsumer(
        MagicMock(), nats_client=MagicMock(), store=MagicMock()
    )
    binds = iter([False, True])

    async def bind():
        if next(binds):
            consumer.subscription = sub
            return True
        return False

    handled = []
    batches = iter(
        [
            nats.errors.TimeoutError(),
            RuntimeError("fetch broke"),
            [_Msg(b"{}"), _Msg(b"{}")],
        ]
    )

    async def fetch(batch, timeout):
        item = next(batches)
        if isinstance(item, Exception):
            raise item
        consumer.running = False
        return item

    async def handle(msg):
        handled.append(msg)
        return "stored"

    sub = MagicMock(fetch=fetch)
    monkeypatch.setattr(consumer, "_bind", bind)
    monkeypatch.setattr(consumer, "handle", handle)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a: real_sleep(0))
    consumer.running = True
    await consumer._run()
    assert len(handled) == 2  # the batch after a timeout and a fetch error


@pytest.mark.asyncio
async def test_start_and_stop_own_the_nats_connection(monkeypatch):
    from data_manager.consumer import llm_call_record_consumer as mod

    nats_client = MagicMock()
    nats_client.connect = AsyncMock(return_value=True)
    nats_client.disconnect = AsyncMock()
    monkeypatch.setattr(mod, "NATSClient", lambda: nats_client)
    consumer = mod.LlmCallRecordConsumer(MagicMock())

    async def idle():
        while consumer.running:
            await __import__("asyncio").sleep(0)

    monkeypatch.setattr(consumer, "_run", idle)
    assert await consumer.start() is True
    assert isinstance(consumer.store, llm.LlmCallRecordStore)
    consumer.subscription = MagicMock(unsubscribe=AsyncMock())
    await consumer.stop()
    nats_client.disconnect.assert_awaited_once()
    consumer.subscription.unsubscribe.assert_awaited_once()

    failing = MagicMock(connect=AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "NATSClient", lambda: failing)
    assert await mod.LlmCallRecordConsumer(MagicMock()).start() is False
