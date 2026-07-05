"""Tests for the MongoDB logical-data-size gauge refresh (data-manager#248, #252).

Verifies the *producer* half of the Atlas M0 data-size leading-indicator alert
(petrosa_k8s#905 / #920 / dm#244 AC6):

- AC1/AC2 — the ``data_manager_mongo_data_size_bytes`` value is cached from a
  mocked ``dbStats().dataSize`` payload on the adapter's connected database
  (and any explicitly named extra DBs) and exported per ``database`` via the
  OTLP ObservableGauge callback.
- AC3 — a ``dbStats`` exception (or an adapter with no connected database)
  does NOT propagate, bumps ``data_manager_mongo_data_size_scrape_errors_total``,
  and leaves the last-known cached value in place.

Per dm#252 the metric is exported through the OTEL meter (OTLP), not the
prometheus_client :9090 registry, so these tests assert on the module cache the
ObservableGauge reads plus the callbacks themselves — not ``prometheus_client.REGISTRY``.

The refresh runs ``dbStats`` on the connected DB via ``adapter.db.command`` —
deliberately NOT ``listDatabases`` (a cluster admin command the app credential
cannot run, and which is absent from the deployed adapter). The fakes below
mirror that exact surface (``db`` / ``db_name`` / ``client``).
"""

from __future__ import annotations

import pytest

from data_manager.maintenance import mongo_data_size_gauge as mdsg


@pytest.fixture(autouse=True)
def _reset_gauge_state():
    """Isolate the module-level OTLP cache/counter between tests."""
    with mdsg._state_lock:
        mdsg._last_data_size.clear()
        mdsg._scrape_errors = 0
    yield


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
    """Read the cached gauge value for a ``database`` label, or None if unset.

    Mirrors what the OTLP ObservableGauge callback exports.
    """
    with mdsg._state_lock:
        return mdsg._last_data_size.get(database)


def _scrape_errors() -> float:
    with mdsg._state_lock:
        return mdsg._scrape_errors


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


@pytest.mark.asyncio
async def test_observable_gauge_callback_yields_one_observation_per_db():
    """The OTLP ObservableGauge callback exports the cached value per database."""
    adapter = _FakeAdapter(
        "petrosa_data_manager",
        _FakeDatabase({"dataSize": 400}),
        client={"petrosa": _FakeDatabase({"dataSize": 25})},
    )
    await mdsg.refresh_mongo_data_size(adapter, extra_databases=("petrosa",))

    observed = {
        obs.attributes["database"]: obs.value for obs in mdsg._observe_data_size(None)
    }

    assert observed == {"petrosa_data_manager": 400, "petrosa": 25}


def test_observable_gauge_callback_yields_nothing_before_first_scrape():
    """No cached sample → the series simply doesn't appear (not a 0)."""
    assert list(mdsg._observe_data_size(None)) == []


@pytest.mark.asyncio
async def test_observable_error_counter_callback_reports_cumulative_total():
    """The OTLP ObservableCounter callback exports the running error total (AC3)."""
    assert [o.value for o in mdsg._observe_scrape_errors(None)] == [0]

    # No connected DB → one scrape error.
    await mdsg.refresh_mongo_data_size(_FakeAdapter("", None))

    assert [o.value for o in mdsg._observe_scrape_errors(None)] == [1]
