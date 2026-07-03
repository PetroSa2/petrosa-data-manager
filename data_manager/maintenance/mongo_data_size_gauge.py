"""MongoDB logical-data-size Prometheus gauge (data-manager#248).

Producer half of the MongoDB Atlas M0 data-size leading-indicator alert
(``petrosa_k8s#905``, itself ``dm#244`` AC6).

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

data-manager is already scraped into Grafana Cloud (its Prometheus registry is
exposed on :9090 — see ``main.py``'s ``start_http_server``). Registering this
gauge on that **same default registry** makes it flow to Grafana Cloud with no
new remote-write, Alloy config, secret, or push CronJob. The downstream alert
(#905) then finalizes as, e.g.::

    sum(data_manager_mongo_data_size_bytes) / (512 * 1024 * 1024) > 0.70  # WARN
    sum(data_manager_mongo_data_size_bytes) / (512 * 1024 * 1024) > 0.85  # P1

The denominator is the 512 MiB constant (M0 exposes no ``_limit`` series).

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
per-database ``dbStats`` failure logs a warning, increments
``data_manager_mongo_data_size_scrape_errors_total``, and leaves the last-known
gauge value in place (Prometheus gauges retain their prior sample until the
next successful set). The refresh is fully async (Motor) so it never blocks the
event loop. Callers should still wrap the ``await`` defensively, but the
function itself is the primary guard.
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

# Module-level so the gauge is registered exactly once on the default registry
# (the one ``start_http_server(METRICS_PORT)`` exposes on :9090). A per-``database``
# label lets the alert ``sum()`` across DBs while still showing which DB
# dominates (``petrosa_data_manager`` is ~99% of the total).
mongo_data_size_bytes = Gauge(
    "data_manager_mongo_data_size_bytes",
    "Logical (uncompressed) MongoDB data size in bytes per application database, "
    "from dbStats().dataSize. This is the figure Atlas M0 gates its 512 MiB quota "
    "on (NOT the compressed storageSize). Producer for petrosa_k8s#905.",
    ["database"],
)

# Companion error counter (AC3): a scrape failure increments this instead of
# crashing the service or zeroing the gauge, so the alert can be made robust to
# transient scrape gaps and operators can see scrape health.
mongo_data_size_scrape_errors = Counter(
    "data_manager_mongo_data_size_scrape_errors_total",
    "Total failed dbStats scrapes while refreshing data_manager_mongo_data_size_bytes "
    "(listDatabases or per-database dbStats). The gauge retains its last-known value.",
)


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
    """Refresh the ``data_manager_mongo_data_size_bytes`` gauge from ``dbStats``.

    Runs ``dbStats`` on the adapter's connected database (and any explicitly
    named ``extra_databases``) and sets the gauge, labelled by ``database``, to
    each database's ``dataSize``. Returns a ``{database: data_size_bytes}`` map
    of the databases successfully sampled (useful for tests and logging).

    Never raises (AC3): a missing connected database or any per-database
    ``dbStats`` error logs a warning, increments
    :data:`mongo_data_size_scrape_errors`, and leaves the last-known gauge
    value(s) untouched. Fully async (Motor), so it never blocks the event loop.

    Args:
        adapter: a connected :class:`~data_manager.db.mongodb_adapter.MongoDBAdapter`
            exposing ``db`` (connected Motor database), ``db_name``, and
            ``client``.
        extra_databases: optional extra database names to also sample by name
            (best-effort; skipped individually on error).
    """
    targets = list(_resolve_targets(adapter, extra_databases))
    if not targets:
        mongo_data_size_scrape_errors.inc()
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
            mongo_data_size_scrape_errors.inc()
            logger.warning(
                "mongo_data_size: dbStats failed for %s; keeping last-known "
                "value for that database: %s",
                name,
                exc,
            )
            continue
        data_size = int((stats or {}).get("dataSize", 0) or 0)
        mongo_data_size_bytes.labels(database=name).set(data_size)
        sampled[name] = data_size

    if sampled:
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
