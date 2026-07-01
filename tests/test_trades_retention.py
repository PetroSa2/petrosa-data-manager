"""Tests for the trades-retention maintenance job (data-manager#246).

The ``trades`` collection stores its ``timestamp`` as an ISO-8601 **string**, so
retention is enforced by lexicographic ``$lt`` comparison rather than a BSON-Date
TTL index. These tests use an in-memory fake collection that implements the exact
Mongo semantics the job relies on (``count_documents`` with ``$lt`` on a string
field, ``find().sort().limit()`` with an ``_id`` projection, and ``delete_many``
with ``_id $in``) so the retention *behaviour* — not just the call shape — is
verified.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data_manager.maintenance import trades_retention as tr


class _FakeCursor:
    """Async-iterable cursor supporting sort/limit/projection over dict docs."""

    def __init__(self, docs: list[dict], ts_field: str):
        self._docs = docs
        self._ts_field = ts_field
        self._sort_field: str | None = None
        self._sort_dir = 1
        self._limit: int | None = None

    def sort(self, field: str, direction: int) -> _FakeCursor:
        self._sort_field = field
        self._sort_dir = direction
        return self

    def limit(self, n: int) -> _FakeCursor:
        self._limit = n
        return self

    def __aiter__(self):
        docs = self._docs
        if self._sort_field is not None:
            docs = sorted(
                docs,
                key=lambda d: d[self._sort_field],
                reverse=self._sort_dir < 0,
            )
        if self._limit is not None:
            docs = docs[: self._limit]
        # Only the projected _id is needed by the job.
        self._iter = iter([{"_id": d["_id"]} for d in docs])
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:  # pragma: no cover - loop terminator
            raise StopAsyncIteration


class _FakeCollection:
    """Minimal in-memory Mongo collection implementing the job's query surface."""

    def __init__(self, docs: list[dict], ts_field: str = "timestamp"):
        self._docs = {d["_id"]: dict(d) for d in docs}
        self._ts_field = ts_field

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for field, cond in query.items():
            value = doc.get(field)
            if isinstance(cond, dict):
                for op, operand in cond.items():
                    if op == "$lt" and not (value < operand):
                        return False
                    if op == "$in" and value not in operand:
                        return False
            elif value != cond:
                return False
        return True

    async def count_documents(self, query: dict) -> int:
        return sum(1 for d in self._docs.values() if self._matches(d, query))

    def find(self, query: dict, projection: dict | None = None) -> _FakeCursor:
        matched = [d for d in self._docs.values() if self._matches(d, query)]
        return _FakeCursor(matched, self._ts_field)

    async def delete_many(self, query: dict):
        to_delete = [_id for _id, d in self._docs.items() if self._matches(d, query)]
        for _id in to_delete:
            del self._docs[_id]

        class _Result:
            deleted_count = len(to_delete)

        return _Result()


class _FakeDB:
    def __init__(self, collections: dict[str, _FakeCollection]):
        self._collections = collections

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections[name]


def _iso(day: int, hour: int = 0) -> str:
    return f"2026-07-{day:02d}T{hour:02d}:00:00Z"


def _make_trades() -> list[dict]:
    # 3 old (June), 2 recent (July 1) relative to a NOW of 2026-07-08.
    return [
        {"_id": 1, "symbol": "BTCUSDT", "timestamp": "2026-06-20T00:00:00Z"},
        {"_id": 2, "symbol": "BTCUSDT", "timestamp": "2026-06-25T12:00:00Z"},
        {"_id": 3, "symbol": "ETHUSDT", "timestamp": "2026-06-30T23:59:59Z"},
        {"_id": 4, "symbol": "BTCUSDT", "timestamp": "2026-07-01T00:00:00Z"},
        {"_id": 5, "symbol": "ETHUSDT", "timestamp": "2026-07-07T08:00:00Z"},
    ]


NOW = datetime(2026, 7, 8, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_resolve_database_name_precedence():
    assert tr.resolve_database_name({"MONGODB_DATABASE": "explicit"}) == "explicit"
    assert tr.resolve_database_name({"MONGODB_DB": "legacy"}) == "legacy"
    assert tr.resolve_database_name({}) == tr.DEFAULT_DATABASE


def test_compute_cutoff_iso_has_no_tz_suffix_and_is_lexicographic_lower_bound():
    cutoff = tr.compute_cutoff_iso(NOW, 7)
    assert cutoff == "2026-07-01T00:00:00"
    # A stored value sharing the second-prefix but carrying a 'Z' is LONGER and
    # therefore compares greater → boundary docs are kept, never deleted early.
    assert "2026-07-01T00:00:00Z" > cutoff


def test_load_config_from_env_clamps_and_parses():
    cfg = tr.load_config_from_env(
        {
            "MONGODB_DATABASE": "db1",
            "TRADES_RETENTION_DAYS": "3",
            "TRADES_RETENTION_BATCH_SIZE": "0",  # clamped to 1
            "TRADES_RETENTION_COLLECTION": "trades",
        }
    )
    assert cfg.database == "db1"
    assert cfg.retention_days == 3
    assert cfg.batch_size == 1
    assert cfg.collection == "trades"


def test_load_config_from_env_ignores_non_integer():
    cfg = tr.load_config_from_env({"TRADES_RETENTION_DAYS": "notanint"})
    # Falls back to the constants default.
    assert cfg.retention_days == tr.constants.TRADES_RETENTION_DAYS


# --------------------------------------------------------------------------- #
# prune_trades behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prune_trades_deletes_only_docs_older_than_window():
    coll = _FakeCollection(_make_trades())
    db = _FakeDB({"trades": coll})
    cfg = tr.TradesRetentionConfig(retention_days=7)

    result = await tr.prune_trades(db, cfg, now=NOW)

    # cutoff = 2026-07-01T00:00:00 → ids 1,2,3 (June) deleted; 4,5 kept (AC5).
    assert result.docs_deleted == 3
    assert result.eligible == 3
    assert set(coll._docs.keys()) == {4, 5}
    assert result.capped is False
    assert result.cutoff_iso == "2026-07-01T00:00:00"


@pytest.mark.asyncio
async def test_prune_trades_dry_run_mutates_nothing():
    coll = _FakeCollection(_make_trades())
    db = _FakeDB({"trades": coll})
    cfg = tr.TradesRetentionConfig(retention_days=7, dry_run=True)

    result = await tr.prune_trades(db, cfg, now=NOW)

    assert result.dry_run is True
    assert result.eligible == 3
    assert result.docs_deleted == 0
    # Nothing deleted.
    assert set(coll._docs.keys()) == {1, 2, 3, 4, 5}


@pytest.mark.asyncio
async def test_prune_trades_noop_when_nothing_stale():
    coll = _FakeCollection(_make_trades())
    db = _FakeDB({"trades": coll})
    # 365-day window → nothing is old enough.
    cfg = tr.TradesRetentionConfig(retention_days=365)

    result = await tr.prune_trades(db, cfg, now=NOW)

    assert result.docs_deleted == 0
    assert result.eligible == 0
    assert result.batches_processed == 0
    assert set(coll._docs.keys()) == {1, 2, 3, 4, 5}


@pytest.mark.asyncio
async def test_prune_trades_batches_and_caps_blast_radius():
    coll = _FakeCollection(_make_trades())
    db = _FakeDB({"trades": coll})
    # batch_size 1, max_batches 2 → only the 2 oldest of the 3 stale docs removed.
    cfg = tr.TradesRetentionConfig(retention_days=7, batch_size=1, max_batches=2)

    result = await tr.prune_trades(db, cfg, now=NOW)

    assert result.docs_deleted == 2
    assert result.batches_processed == 2
    assert result.capped is True
    # The two OLDEST (ids 1 and 2) go first; id 3 remains for the next run.
    assert set(coll._docs.keys()) == {3, 4, 5}


@pytest.mark.asyncio
async def test_prune_trades_is_idempotent():
    coll = _FakeCollection(_make_trades())
    db = _FakeDB({"trades": coll})
    cfg = tr.TradesRetentionConfig(retention_days=7)

    first = await tr.prune_trades(db, cfg, now=NOW)
    second = await tr.prune_trades(db, cfg, now=NOW)

    assert first.docs_deleted == 3
    assert second.docs_deleted == 0
    assert second.eligible == 0
    assert set(coll._docs.keys()) == {4, 5}


@pytest.mark.asyncio
async def test_prune_trades_respects_custom_collection_and_field():
    docs = [
        {"_id": 1, "trade_time": "2026-06-01T00:00:00Z"},
        {"_id": 2, "trade_time": "2026-07-07T00:00:00Z"},
    ]
    coll = _FakeCollection(docs, ts_field="trade_time")
    db = _FakeDB({"agg_trades": coll})
    cfg = tr.TradesRetentionConfig(
        collection="agg_trades", ts_field="trade_time", retention_days=7
    )

    result = await tr.prune_trades(db, cfg, now=NOW)

    assert result.docs_deleted == 1
    assert set(coll._docs.keys()) == {2}
