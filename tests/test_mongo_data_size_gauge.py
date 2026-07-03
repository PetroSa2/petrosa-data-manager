"""Tests for the MongoDB logical-data-size gauge refresh (data-manager#248).

Verifies the *producer* half of the Atlas M0 data-size leading-indicator alert
(petrosa_k8s#905 / dm#244 AC6):

- AC1/AC2 — the ``data_manager_mongo_data_size_bytes`` gauge is set per
  application database from a mocked ``dbStats().dataSize`` payload, and system
  databases (admin/local/config) are excluded.
- AC3/AC4 — a ``dbStats`` exception (or a top-level ``listDatabases`` failure)
  does NOT propagate, bumps ``data_manager_mongo_data_size_scrape_errors_total``,
  and leaves the last-known gauge value in place.

The tests drive a fake Motor-style adapter exposing the exact async surface
``refresh_mongo_data_size`` relies on (``list_databases`` / ``db_stats``).
"""

from __future__ import annotations

import pytest

from data_manager.maintenance import mongo_data_size_gauge as mdsg


class _FakeAdapter:
    """Minimal async stand-in for MongoDBAdapter's introspection surface.

    ``databases`` is the ``listDatabases`` payload shape (list of dicts with a
    ``name``); ``stats`` maps db-name → the ``dbStats`` dict to return, or an
    ``Exception`` instance to raise for that database. ``list_raises`` forces
    the top-level ``list_databases`` call to raise.
    """

    def __init__(self, databases, stats, *, list_raises: Exception | None = None):
        self._databases = databases
        self._stats = stats
        self._list_raises = list_raises

    async def list_databases(self):
        if self._list_raises is not None:
            raise self._list_raises
        return self._databases

    async def db_stats(self, name):
        value = self._stats[name]
        if isinstance(value, Exception):
            raise value
        return value


def _gauge_value(database: str):
    """Read the current gauge value for a ``database`` label, or None if unset."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "data_manager_mongo_data_size_bytes", {"database": database}
    )


def _scrape_errors() -> float:
    return mdsg.mongo_data_size_scrape_errors._value.get()


@pytest.mark.asyncio
async def test_refresh_sets_gauge_per_app_db_and_skips_system_dbs():
    adapter = _FakeAdapter(
        databases=[
            {"name": "petrosa_data_manager"},
            {"name": "petrosa"},
            {"name": "admin"},
            {"name": "local"},
            {"name": "config"},
        ],
        stats={
            "petrosa_data_manager": {"dataSize": 345_000_000},
            "petrosa": {"dataSize": 12_000_000},
            # System DBs must never be queried; give them raising values to
            # prove they are skipped before db_stats is called.
            "admin": RuntimeError("should not be queried"),
            "local": RuntimeError("should not be queried"),
            "config": RuntimeError("should not be queried"),
        },
    )

    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {
        "petrosa_data_manager": 345_000_000,
        "petrosa": 12_000_000,
    }
    assert _gauge_value("petrosa_data_manager") == 345_000_000
    assert _gauge_value("petrosa") == 12_000_000
    # System DBs never set a series.
    assert _gauge_value("admin") is None


@pytest.mark.asyncio
async def test_missing_datasize_defaults_to_zero():
    adapter = _FakeAdapter(
        databases=[{"name": "empty_db"}],
        stats={"empty_db": {"storageSize": 100}},  # no dataSize key
    )

    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {"empty_db": 0}
    assert _gauge_value("empty_db") == 0


@pytest.mark.asyncio
async def test_dbstats_exception_is_isolated_and_does_not_propagate():
    # First, a clean refresh establishes a known value.
    good = _FakeAdapter(
        databases=[{"name": "iso_db"}],
        stats={"iso_db": {"dataSize": 500}},
    )
    await mdsg.refresh_mongo_data_size(good)
    assert _gauge_value("iso_db") == 500

    errors_before = _scrape_errors()

    # Now dbStats raises for that DB. The call must not raise, the error
    # counter must advance, and the last-known gauge value must be retained.
    broken = _FakeAdapter(
        databases=[{"name": "iso_db"}],
        stats={"iso_db": PermissionError("not authorized on iso_db")},
    )
    refreshed = await mdsg.refresh_mongo_data_size(broken)

    assert refreshed == {}  # nothing refreshed this pass
    assert _scrape_errors() == errors_before + 1
    assert _gauge_value("iso_db") == 500  # last-known value retained (AC3)


@pytest.mark.asyncio
async def test_one_failing_db_does_not_block_the_others():
    adapter = _FakeAdapter(
        databases=[{"name": "good_db"}, {"name": "bad_db"}],
        stats={
            "good_db": {"dataSize": 777},
            "bad_db": RuntimeError("transient"),
        },
    )

    errors_before = _scrape_errors()
    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {"good_db": 777}
    assert _gauge_value("good_db") == 777
    assert _scrape_errors() == errors_before + 1


@pytest.mark.asyncio
async def test_list_databases_failure_is_isolated():
    errors_before = _scrape_errors()
    adapter = _FakeAdapter(
        databases=[],
        stats={},
        list_raises=ConnectionError("mongo unreachable"),
    )

    refreshed = await mdsg.refresh_mongo_data_size(adapter)

    assert refreshed == {}
    assert _scrape_errors() == errors_before + 1
