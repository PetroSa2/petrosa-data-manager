"""Tests for the MongoDB logical-data-size gauge refresh (data-manager#248).

Verifies the *producer* half of the Atlas M0 data-size leading-indicator alert
(petrosa_k8s#905 / dm#244 AC6):

- AC1/AC2 — the ``data_manager_mongo_data_size_bytes`` gauge is set from a
  mocked ``dbStats().dataSize`` payload on the adapter's connected database
  (and any explicitly named extra DBs).
- AC3/AC4 — a ``dbStats`` exception (or an adapter with no connected database)
  does NOT propagate, bumps ``data_manager_mongo_data_size_scrape_errors_total``,
  and leaves the last-known gauge value in place.

The refresh runs ``dbStats`` on the connected DB via ``adapter.db.command`` —
deliberately NOT ``listDatabases`` (a cluster admin command the app credential
cannot run, and which is absent from the deployed adapter). The fakes below
mirror that exact surface (``db`` / ``db_name`` / ``client``).
"""

from __future__ import annotations

import pytest

from data_manager.maintenance import mongo_data_size_gauge as mdsg


class _FakeDatabase:
    """Async stand-in for a Motor database exposing ``command('dbStats')``.

    ``stats`` is the dict to return, or an ``Exception`` instance to raise.
    """

    def __init__(self, stats):
        self._stats = stats

    async def command(self, name):
        assert name == "dbStats"
        if isinstance(self._stats, Exception):
            raise self._stats
        return self._stats


class _FakeAdapter:
    """Minimal stand-in for MongoDBAdapter's connected surface.

    ``db_name`` + ``db`` model the connected database; ``client`` maps extra
    database names to :class:`_FakeDatabase` handles (as ``client[name]``).
    """

    def __init__(self, db_name, db, *, client=None):
        self.db_name = db_name
        self.db = db
        self.client = client or {}


def _gauge_value(database: str):
    """Read the current gauge value for a ``database`` label, or None if unset."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "data_manager_mongo_data_size_bytes", {"database": database}
    )


def _scrape_errors() -> float:
    return mdsg.mongo_data_size_scrape_errors._value.get()


@pytest.mark.asyncio
async def test_refresh_sets_gauge_for_connected_db():
    adapter = _FakeAdapter(
        "petrosa_data_manager", _FakeDatabase({"dataSize": 345_000_000})
    )

    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {"petrosa_data_manager": 345_000_000}
    assert _gauge_value("petrosa_data_manager") == 345_000_000


@pytest.mark.asyncio
async def test_refresh_samples_extra_databases_by_name():
    adapter = _FakeAdapter(
        "petrosa_data_manager",
        _FakeDatabase({"dataSize": 300}),
        client={"petrosa": _FakeDatabase({"dataSize": 25})},
    )

    refreshed = await mdsg.refresh_mongo_data_size(
        adapter, extra_databases=("petrosa",)
    )

    assert refreshed == {"petrosa_data_manager": 300, "petrosa": 25}
    assert _gauge_value("petrosa_data_manager") == 300
    assert _gauge_value("petrosa") == 25


@pytest.mark.asyncio
async def test_missing_datasize_defaults_to_zero():
    adapter = _FakeAdapter("empty_db", _FakeDatabase({"storageSize": 100}))

    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {"empty_db": 0}
    assert _gauge_value("empty_db") == 0


@pytest.mark.asyncio
async def test_dbstats_exception_is_isolated_and_retains_last_value():
    # A clean refresh establishes a known value.
    good = _FakeAdapter("iso_db", _FakeDatabase({"dataSize": 500}))
    await mdsg.refresh_mongo_data_size(good)
    assert _gauge_value("iso_db") == 500

    errors_before = _scrape_errors()

    # Now dbStats raises. The call must not raise, the error counter must
    # advance, and the last-known gauge value must be retained (AC3).
    broken = _FakeAdapter(
        "iso_db", _FakeDatabase(PermissionError("not authorized on iso_db"))
    )
    refreshed = await mdsg.refresh_mongo_data_size(broken)

    assert refreshed == {}
    assert _scrape_errors() == errors_before + 1
    assert _gauge_value("iso_db") == 500


@pytest.mark.asyncio
async def test_one_failing_extra_db_does_not_block_connected_db():
    adapter = _FakeAdapter(
        "good_db",
        _FakeDatabase({"dataSize": 777}),
        client={"bad_db": _FakeDatabase(RuntimeError("transient"))},
    )

    errors_before = _scrape_errors()
    refreshed = await mdsg.refresh_mongo_data_size(adapter, extra_databases=("bad_db",))

    assert refreshed == {"good_db": 777}
    assert _gauge_value("good_db") == 777
    assert _scrape_errors() == errors_before + 1


@pytest.mark.asyncio
async def test_no_connected_database_is_isolated():
    errors_before = _scrape_errors()
    # Adapter never connected: db is None, db_name empty.
    adapter = _FakeAdapter("", None)

    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {}
    assert _scrape_errors() == errors_before + 1


@pytest.mark.asyncio
async def test_extra_db_duplicate_of_connected_is_skipped():
    # Passing the connected DB name again must not double-count it.
    adapter = _FakeAdapter(
        "petrosa_data_manager",
        _FakeDatabase({"dataSize": 100}),
        client={"petrosa_data_manager": _FakeDatabase({"dataSize": 999})},
    )

    refreshed = await mdsg.refresh_mongo_data_size(
        adapter, extra_databases=("petrosa_data_manager",)
    )

    assert refreshed == {"petrosa_data_manager": 100}
