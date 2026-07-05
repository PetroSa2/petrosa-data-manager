"""MongoDB logical-data-size gauge, exported via OTLP (data-manager#248, #252).

Producer half of the MongoDB Atlas M0 data-size leading-indicator alert
(``petrosa_k8s#905`` / ``petrosa_k8s#920``, itself ``dm#244`` AC6).

Why this exists
---------------
MongoDB Atlas M0 enforces its 512 MiB quota on the **logical, uncompressed**
``dbStats().dataSize`` — NOT the compressed ``storageSize`` reported as the
"on-disk" figure. Every one of the four 2026 production quota P0s
(06-10 / 06-16 / 06-19 / 07-01) was diagnosed by running ``dbStats`` from the
application and reading ``dataSize``. There is **no** Atlas data-size series in
Grafana Cloud and there never will be on M0 — the official Grafana Cloud Atlas
integration requires M10+. So the only reliable source of the number the quota
actually gates on is ``db.command("dbStats")["dataSize"]`` from the app itself.

Why OTLP, not the prometheus_client :9090 registry (dm#252)
-----------------------------------------------------------
dm#248 originally registered this gauge on the ``prometheus_client`` default
registry exposed on :9090, assuming that endpoint was scraped into Grafana
Cloud. It is **not**: data-manager reaches Grafana Cloud exclusively through the
**OTLP pipeline** (``petrosa-otel`` → otelcol → ``grafanacloud-prom``). Nothing
scrapes :9090, so the gauge was live on the pod but ``0 series`` in Grafana
Cloud (live-verified 2026-07-04), leaving petrosa_k8s#920/#905 permanently
``NoData``. This module therefore emits the metric through the **OTEL meter**
(``get_meter`` → OTLP), exactly like the ``data_manager_decision_*`` counters,
so it lands in ``grafanacloud-prom`` under the same name and the existing alert
exprs work unchanged::

    sum(data_manager_mongo_data_size_bytes) / (512 * 1024 * 1024) > 0.80  # WARN
    sum(data_manager_mongo_data_size_bytes) / (512 * 1024 * 1024) > 0.95  # P1

The denominator is the 512 MiB constant (M0 exposes no ``_limit`` series).

Async scrape vs. sync export (the ObservableGauge idiom)
--------------------------------------------------------
An OTEL ``ObservableGauge`` reports via a **synchronous** callback the SDK
invokes at export time — it cannot ``await`` Motor. So the async refresh loop
(:func:`refresh_mongo_data_size`, driven from ``main.py`` every
``MONGO_DATA_SIZE_REFRESH_INTERVAL`` s) writes the latest ``dbStats().dataSize``
per database into a small module-level cache, and the observable callback reads
that cache and yields one :class:`Observation` per database at export. The
cache is guarded by a :class:`threading.Lock` because the callback runs on the
SDK's exporter thread while the loop writes from the asyncio event-loop thread.
The scrape-error total is exported the same way (an ``ObservableCounter`` over a
running integer), so it too reaches Grafana Cloud (AC3).

Why the connected DB, not ``listDatabases``
-------------------------------------------
The refresh runs ``dbStats`` on the adapter's **already-connected** database
(``petrosa_data_manager``, ~99% of the cluster's logical size), not an
enumeration via ``listDatabases``. ``listDatabases`` is a cluster-level admin
command the application's Mongo credential is not granted — the read-only
storage-inventory audit needs a *dedicated* clusterMonitor credential for
exactly that. Running ``dbStats`` on the connected DB is an ordinary read the
app can always do, and it is what every P0 diagnosis actually used. Extra
databases can be sampled by explicit name when needed.

Failure isolation (AC3)
-----------------------
:func:`refresh_mongo_data_size` never raises. A missing connected database or a
per-database ``dbStats`` failure logs a warning, increments the exported
``data_manager_mongo_data_size_scrape_errors_total`` counter, and leaves the
last-known cached value in place (so the exported gauge holds its prior sample
until the next successful scrape). The refresh is fully async (Motor) so it
never blocks the event loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from typing import Any

from opentelemetry.metrics import CallbackOptions, Observation

try:
    from petrosa_otel import get_meter
except ImportError:  # pragma: no cover - petrosa_otel always present in prod
    from opentelemetry import metrics as _otel_metrics

    def get_meter(name: str) -> Any:
        return _otel_metrics.get_meter(name)


logger = logging.getLogger(__name__)

_meter = get_meter(__name__)

# Latest per-database ``dbStats().dataSize`` written by the async refresh loop
# and read by the ObservableGauge callback at OTLP export time. Guarded by a
# lock because the two run on different threads (asyncio event loop vs. the
# SDK's metric-exporter thread). ``_scrape_errors`` is a monotonic running
# total exported via an ObservableCounter (AC3).
_state_lock = threading.Lock()
_last_data_size: dict[str, int] = {}
_scrape_errors: int = 0


def _observe_data_size(_options: CallbackOptions) -> Iterable[Observation]:
    """ObservableGauge callback: yield the last-known size per database.

    Runs synchronously on the SDK exporter thread; reads only the cache the
    async refresh loop maintains. Yields nothing until the first successful
    scrape (so the series simply doesn't appear rather than reporting 0).
    """
    with _state_lock:
        snapshot = dict(_last_data_size)
    for database, data_size in snapshot.items():
        yield Observation(data_size, {"database": database})


def _observe_scrape_errors(_options: CallbackOptions) -> Iterable[Observation]:
    """ObservableCounter callback: yield the cumulative scrape-error count."""
    with _state_lock:
        yield Observation(_scrape_errors)


# Keep module-level references so the instruments (and their callbacks) are not
# garbage-collected. ``data_manager_mongo_data_size_bytes`` is emitted with no
# ``unit`` on purpose: the OTLP→Prometheus exporter appends a unit suffix (e.g.
# ``By`` → ``_bytes``) and the name already ends in ``_bytes`` — mirroring how
# ``data_manager_decision_messages_received_total`` keeps its exact name.
mongo_data_size_bytes = _meter.create_observable_gauge(
    "data_manager_mongo_data_size_bytes",
    callbacks=[_observe_data_size],
    description=(
        "Logical (uncompressed) MongoDB data size in bytes per application "
        "database, from dbStats().dataSize. This is the figure Atlas M0 gates "
        "its 512 MiB quota on (NOT the compressed storageSize). Producer for "
        "petrosa_k8s#905 / #920; exported via OTLP (dm#252)."
    ),
)

mongo_data_size_scrape_errors = _meter.create_observable_counter(
    "data_manager_mongo_data_size_scrape_errors_total",
    callbacks=[_observe_scrape_errors],
    description=(
        "Cumulative failed dbStats scrapes while refreshing "
        "data_manager_mongo_data_size_bytes. The gauge retains its last-known "
        "value across failures."
    ),
)


def _record_scrape_error() -> None:
    global _scrape_errors
    with _state_lock:
        _scrape_errors += 1


def _resolve_targets(adapter: Any, extra_databases: tuple[str, ...]):
    """Yield ``(db_name, motor_database_handle)`` pairs to sample.

    Primary target is the adapter's **already-connected** database
    (``adapter.db`` / ``adapter.db_name``) — ``petrosa_data_manager``, which is
    ~99% of the cluster's logical size. Additional databases can be named
    explicitly via ``extra_databases`` and are resolved through
    ``adapter.client[name]``.

    Deliberately does NOT call ``listDatabases``: that is a cluster-level admin
    command the application's Mongo credential is not granted (the read-only
    storage-inventory audit needs a *dedicated* clusterMonitor credential for
    exactly that reason — see ``storage_inventory``), and enumerating every DB
    is unnecessary when the app owns the one that dominates the quota. Running
    ``dbStats`` on the connected DB is an ordinary read the app can always do.
    """
    seen: set[str] = set()
    primary_name = getattr(adapter, "db_name", None)
    primary_db = getattr(adapter, "db", None)
    if primary_name and primary_db is not None:
        seen.add(primary_name)
        yield primary_name, primary_db

    client = getattr(adapter, "client", None)
    for name in extra_databases:
        name = (name or "").strip()
        if not name or name in seen or client is None:
            continue
        seen.add(name)
        yield name, client[name]


async def refresh_mongo_data_size(
    adapter: Any,
    *,
    extra_databases: tuple[str, ...] = (),
) -> dict[str, int]:
    """Refresh the cached ``data_manager_mongo_data_size_bytes`` values.

    Runs ``dbStats`` on the adapter's connected database (and any explicitly
    named ``extra_databases``) and stores each database's ``dataSize`` in the
    module cache the OTLP ObservableGauge exports. Returns a
    ``{database: data_size_bytes}`` map of the databases successfully sampled
    (useful for tests and logging).

    Never raises (AC3): a missing connected database or any per-database
    ``dbStats`` error logs a warning, increments the exported
    ``data_manager_mongo_data_size_scrape_errors_total`` counter, and leaves the
    last-known cached value(s) untouched. Fully async (Motor), so it never
    blocks the event loop.

    Args:
        adapter: a connected :class:`~data_manager.db.mongodb_adapter.MongoDBAdapter`
            exposing ``db`` (connected Motor database), ``db_name``, and
            ``client``.
        extra_databases: optional extra database names to also sample by name
            (best-effort; skipped individually on error).
    """
    targets = list(_resolve_targets(adapter, extra_databases))
    if not targets:
        _record_scrape_error()
        logger.warning(
            "mongo_data_size: adapter exposes no connected database "
            "(db/db_name unset); leaving last-known gauge value(s) in place",
        )
        return {}

    sampled: dict[str, int] = {}
    for name, db_handle in targets:
        try:
            stats = await db_handle.command("dbStats")
        except Exception as exc:  # noqa: BLE001 — isolate per-DB failures (AC3)
            _record_scrape_error()
            logger.warning(
                "mongo_data_size: dbStats failed for %s; keeping last-known "
                "value for that database: %s",
                name,
                exc,
            )
            continue
        data_size = int((stats or {}).get("dataSize", 0) or 0)
        sampled[name] = data_size

    if sampled:
        with _state_lock:
            _last_data_size.update(sampled)
        total = sum(sampled.values())
        logger.info(
            "mongo_data_size: refreshed %d database(s); total dataSize=%d bytes "
            "(%.1f MiB) — %s",
            len(sampled),
            total,
            total / (1024 * 1024),
            ", ".join(f"{k}={v}" for k, v in sorted(sampled.items())),
        )
    return sampled
