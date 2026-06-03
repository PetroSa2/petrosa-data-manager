"""
Read-only storage-inventory audit (petrosa_k8s#794, leaf of #783).

Enumerates every reachable MongoDB database + collection and every MySQL
schema + table, attributing on-disk storage so the operator can root-cause
the >400 MB residual after the manual klines cleanup that unblocked the P0
storage incident.

This module is **strictly read-only**. The guarantee is two layers:

1. **Credential** (the real guarantee): the Job must connect with a
   ``SELECT``-only MySQL user and a Mongo role limited to ``read`` +
   ``listDatabases``/``clusterMonitor``. The module itself cannot enforce
   this — only the operator provisioning the credential can.
2. **Code-level**: this module issues ONLY ``listDatabases``/``dbStats``/
   ``collStats``/``list_collections``/``find_one`` (Mongo) and
   ``information_schema`` SELECTs (MySQL). It deliberately bypasses
   :meth:`MySQLAdapter.connect` (which runs ``_create_tables`` DDL) by
   using :func:`data_manager.db.mysql_adapter.create_read_only_engine`.

Invocation::

    python -m data_manager.maintenance.storage_inventory [--json]
                                                          [--mongo-only]
                                                          [--mysql-only]

Exit codes (petrosa_k8s#794 AC7):

* ``0``  — both backends OK (or the selected subset is OK)
* ``10`` — Mongo backend failed
* ``11`` — MySQL backend failed
* ``12`` — both backends failed
* ``13`` — privilege/permission denied on a required admin command

See ``docs/storage-inventory.md`` for the operator runbook + report
template, and ``_bmad-output/implementation-artifacts/tech-spec-storage-inventory-audit-mongo-mysql.md``
for the full design rationale + adversarial-review notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import (
    create_read_only_engine,
    list_schemas,
    table_inventory,
)

logger = logging.getLogger(__name__)

# Atlas/Mongo system DBs we report separately rather than treating as app data
SYSTEM_DBS = frozenset({"admin", "local", "config"})

# Time-series collection naming — the binance-data-extractor writes these
# as MongoDB time-series collections, whose bytes live in a hidden
# ``system.buckets.<name>``. The audit must size the backing bucket too.
TIMESERIES_PREFIXES = ("klines_",)

# Known writers map (from the tech-spec "Known store inventory" section,
# 2026-06-03). Used to flag `orphan-suspect` stores (live with no known
# writer) without ever auto-deleting. This is best-effort intelligence,
# not a hard rule.
KNOWN_MONGO_COLLECTIONS = frozenset(
    {
        # per-symbol/per-timeframe append-heavy
        # (matched by prefix, see _is_known_mongo_collection)
        # lifecycle/registry/config
        "intents",
        "cio_decisions",
        "execution_events",
        "pnl_events",
        "signals",
        "alerts",
        "characterizations",
        "characterization_artifacts",
        "drawdown_breaches",
        "envelopes",
        "pending_envelope_changes",
        "leverage_bounds",
        "strategy_registry",
        "schemas",
        "app_config",
        "app_config_audit",
        "strategy_configs_global",
        "strategy_configs_symbol",
        "strategy_config_audit",
        "strategy_lifecycle_events",
        "trading_configs_global",
        "trading_configs_symbol",
        "trading_configs_symbol_side",
        "trading_configs_audit",
        "leverage_status",
        "distributed_locks",
        "leader_election",
    }
)

KNOWN_MONGO_PREFIXES = (
    "candles_",
    "klines_",
    "trades_",
    "funding_",
    "tickers_",
    "depth_",
    "system.buckets.",  # backing storage for time-series collections
    "system.views",  # MongoDB metadata
)

KNOWN_MYSQL_TABLES = frozenset(
    {
        "positions",
        "strategy_positions",
        "exchange_positions",
        "daily_pnl",
        "signals",
        "datasets",
        "audit_logs",
        "health_metrics",
        "schemas",
        "backfill_jobs",
        "lineage_records",
    }
)

KNOWN_MYSQL_PREFIXES = ("klines_",)


# Exit codes (petrosa_k8s#794 AC7)
EXIT_OK = 0
EXIT_MONGO_FAILED = 10
EXIT_MYSQL_FAILED = 11
EXIT_BOTH_FAILED = 12
EXIT_PRIVILEGE_DENIED = 13


# ---------------------------------------------------------------------------
# Dataclasses for the structured report
# ---------------------------------------------------------------------------


@dataclass
class MongoCollectionStat:
    db_name: str
    name: str
    is_timeseries: bool
    storage_size: int  # on-disk compressed — the Atlas-quota figure
    data_size: int  # logical
    total_index_size: int
    count: int
    n_indexes: int
    avg_obj_size: int
    backing_bucket_storage_size: int | None  # only set for time-series
    backing_bucket_data_size: int | None
    newest_doc_age: str | None  # ISO-format timestamp or None
    classification: str  # live | orphan-suspect | duplicated
    error: str | None = None


@dataclass
class MongoDatabaseStat:
    name: str
    is_system: bool
    storage_size: int  # dbStats.storageSize (on-disk)
    data_size: int  # dbStats.dataSize (logical)
    index_size: int  # dbStats.indexSize
    collections: int
    objects: int
    collection_stats: list[MongoCollectionStat] = field(default_factory=list)
    error: str | None = None


@dataclass
class MysqlTableStat:
    schema: str
    name: str
    table_type: str  # 'BASE TABLE' or 'VIEW'
    data_length: int
    index_length: int
    table_rows_estimated: int
    create_time: str | None
    update_time: str | None
    classification: str  # live | orphan-suspect | duplicated


@dataclass
class MysqlSchemaStat:
    name: str
    tables: list[MysqlTableStat] = field(default_factory=list)
    error: str | None = None


@dataclass
class StorageInventoryReport:
    generated_at: str
    mongo_ok: bool
    mysql_ok: bool
    mongo_error: str | None
    mysql_error: str | None
    mongo_databases: list[MongoDatabaseStat] = field(default_factory=list)
    mongo_oplog: dict | None = None
    mysql_schemas: list[MysqlSchemaStat] = field(default_factory=list)
    # Pre-computed root-cause attribution (petrosa_k8s#794 AC3)
    attribution: dict = field(default_factory=dict)
    duplicated_candle_timeframes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _is_known_mongo_collection(name: str) -> bool:
    if name in KNOWN_MONGO_COLLECTIONS:
        return True
    return any(name.startswith(p) for p in KNOWN_MONGO_PREFIXES)


def _is_known_mysql_table(name: str) -> bool:
    if name in KNOWN_MYSQL_TABLES:
        return True
    return any(name.startswith(p) for p in KNOWN_MYSQL_PREFIXES)


def _classify_mongo(name: str, duplicated: bool) -> str:
    if duplicated:
        return "duplicated"
    if _is_known_mongo_collection(name):
        return "live"
    return "orphan-suspect"


def _classify_mysql(name: str, duplicated: bool) -> str:
    if duplicated:
        return "duplicated"
    if _is_known_mysql_table(name):
        return "live"
    return "orphan-suspect"


def _normalize_timeframe(name: str) -> str | None:
    """Return a canonical timeframe label for cross-backend duplication checks.

    ``klines_1m`` (Mongo time-series), ``candles_BTCUSDT_1m``, and MySQL
    ``klines_m1`` should all map to ``1m`` so a timeframe present in
    multiple backends/namings is flagged ``duplicated``.
    """
    if name.startswith("klines_"):
        tf = name[len("klines_") :]
        # MySQL klines_m1 / klines_h1 / klines_d1 → 1m / 1h / 1d
        if tf and tf[0] in {"m", "h", "d"} and tf[1:].isdigit():
            return f"{tf[1:]}{tf[0]}"
        if tf:
            return tf
        return None
    if name.startswith("candles_"):
        parts = name.split("_")
        if len(parts) >= 3:
            return parts[-1]
    return None


# ---------------------------------------------------------------------------
# Mongo audit
# ---------------------------------------------------------------------------


async def audit_mongo(
    adapter: MongoDBAdapter,
) -> tuple[list[MongoDatabaseStat], dict | None, str | None]:
    """Walk every Mongo database and emit per-db / per-collection stats.

    Returns ``(database_stats, oplog_stats, error)`` — ``error`` is non-None
    when the top-level ``listDatabases`` call fails (driving exit-code
    decisions in :func:`main`).
    """
    try:
        dbs = await adapter.list_databases()
    except Exception as exc:  # noqa: BLE001 — surface raw error to caller
        logger.error("storage_inventory: listDatabases failed: %s", exc)
        return [], None, str(exc)

    out: list[MongoDatabaseStat] = []
    for db_info in dbs:
        name = db_info.get("name", "?")
        is_system = name in SYSTEM_DBS
        try:
            stats = await adapter.db_stats(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("storage_inventory: dbStats failed for %s: %s", name, exc)
            out.append(
                MongoDatabaseStat(
                    name=name,
                    is_system=is_system,
                    storage_size=int(db_info.get("sizeOnDisk", 0) or 0),
                    data_size=0,
                    index_size=0,
                    collections=0,
                    objects=0,
                    error=str(exc),
                )
            )
            continue

        db_stat = MongoDatabaseStat(
            name=name,
            is_system=is_system,
            storage_size=int(stats.get("storageSize", 0) or 0),
            data_size=int(stats.get("dataSize", 0) or 0),
            index_size=int(stats.get("indexSize", 0) or 0),
            collections=int(stats.get("collections", 0) or 0),
            objects=int(stats.get("objects", 0) or 0),
        )

        # Enumerate collections. The system DBs intentionally surface
        # `oplog.rs`-like collections here so their footprint is visible.
        try:
            target_db = adapter.client[name]
            coll_names = await target_db.list_collection_names()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "storage_inventory: list_collection_names failed for %s: %s",
                name,
                exc,
            )
            db_stat.error = str(exc)
            out.append(db_stat)
            continue

        for coll_name in sorted(coll_names):
            stat = await _audit_mongo_collection(adapter, name, coll_name)
            db_stat.collection_stats.append(stat)

        out.append(db_stat)

    try:
        oplog = await adapter.oplog_size()
    except Exception as exc:  # noqa: BLE001 — oplog is best-effort
        logger.info("storage_inventory: oplog_size unavailable: %s", exc)
        oplog = None

    return out, oplog, None


async def _audit_mongo_collection(
    adapter: MongoDBAdapter, db_name: str, coll_name: str
) -> MongoCollectionStat:
    """Read ``collStats`` (+ TS bucket sizing + newest-doc age) for one collection."""
    is_ts = False
    try:
        is_ts = await adapter.is_timeseries(db_name, coll_name)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "storage_inventory: is_timeseries failed for %s.%s: %s",
            db_name,
            coll_name,
            exc,
        )

    try:
        stats = await adapter.coll_stats(db_name, coll_name)
    except Exception as exc:  # noqa: BLE001
        return MongoCollectionStat(
            db_name=db_name,
            name=coll_name,
            is_timeseries=is_ts,
            storage_size=0,
            data_size=0,
            total_index_size=0,
            count=0,
            n_indexes=0,
            avg_obj_size=0,
            backing_bucket_storage_size=None,
            backing_bucket_data_size=None,
            newest_doc_age=None,
            classification="error",
            error=str(exc),
        )

    backing_storage = None
    backing_data = None
    if is_ts:
        bucket_name = f"system.buckets.{coll_name}"
        try:
            bstats = await adapter.coll_stats(db_name, bucket_name)
            backing_storage = int(bstats.get("storageSize", 0) or 0)
            backing_data = int(bstats.get("size", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "storage_inventory: backing bucket missing for %s.%s: %s",
                db_name,
                coll_name,
                exc,
            )

    try:
        newest_dt = await adapter.newest_doc_age(db_name, coll_name)
    except Exception:  # noqa: BLE001
        newest_dt = None

    return MongoCollectionStat(
        db_name=db_name,
        name=coll_name,
        is_timeseries=is_ts,
        storage_size=int(stats.get("storageSize", 0) or 0),
        data_size=int(stats.get("size", 0) or 0),
        total_index_size=int(stats.get("totalIndexSize", 0) or 0),
        count=int(stats.get("count", 0) or 0),
        n_indexes=int(stats.get("nindexes", 0) or 0),
        avg_obj_size=int(stats.get("avgObjSize", 0) or 0),
        backing_bucket_storage_size=backing_storage,
        backing_bucket_data_size=backing_data,
        newest_doc_age=newest_dt.isoformat() if newest_dt else None,
        classification="live",  # final classification assigned in _classify_all
    )


# ---------------------------------------------------------------------------
# MySQL audit
# ---------------------------------------------------------------------------


def audit_mysql(connection_string: str) -> tuple[list[MysqlSchemaStat], str | None]:
    """Enumerate every non-system MySQL schema and emit per-table size info."""
    engine = None
    try:
        engine = create_read_only_engine(connection_string)
        schemas = list_schemas(engine)
    except Exception as exc:  # noqa: BLE001
        logger.error("storage_inventory: MySQL schema enumeration failed: %s", exc)
        if engine is not None:
            engine.dispose()
        return [], str(exc)

    out: list[MysqlSchemaStat] = []
    for schema in schemas:
        try:
            rows = table_inventory(engine, schema)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "storage_inventory: table_inventory failed for %s: %s", schema, exc
            )
            out.append(MysqlSchemaStat(name=schema, error=str(exc)))
            continue

        schema_stat = MysqlSchemaStat(name=schema)
        for row in rows:
            schema_stat.tables.append(
                MysqlTableStat(
                    schema=schema,
                    name=row["TABLE_NAME"],
                    table_type=row.get("TABLE_TYPE") or "BASE TABLE",
                    data_length=int(row.get("DATA_LENGTH") or 0),
                    index_length=int(row.get("INDEX_LENGTH") or 0),
                    table_rows_estimated=int(row.get("TABLE_ROWS") or 0),
                    create_time=_isoformat_or_none(row.get("CREATE_TIME")),
                    update_time=_isoformat_or_none(row.get("UPDATE_TIME")),
                    classification="live",  # final classification assigned later
                )
            )
        out.append(schema_stat)

    engine.dispose()
    return out, None


def _isoformat_or_none(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


# ---------------------------------------------------------------------------
# Cross-backend classification + attribution
# ---------------------------------------------------------------------------


def _classify_all(report: StorageInventoryReport) -> None:
    """Mutate the report in-place to set classification + duplication flags.

    Cross-backend duplication: a candle/kline timeframe is `duplicated` when
    the same canonical timeframe is present in more than one backend (Mongo
    `klines_*`, MySQL `klines_*`, Mongo `candles_*`).
    """
    mongo_timeframes: set[str] = set()
    mysql_timeframes: set[str] = set()

    for db in report.mongo_databases:
        for c in db.collection_stats:
            tf = _normalize_timeframe(c.name)
            if tf:
                mongo_timeframes.add(tf)

    for schema in report.mysql_schemas:
        for t in schema.tables:
            if t.table_type == "VIEW":
                continue
            tf = _normalize_timeframe(t.name)
            if tf:
                mysql_timeframes.add(tf)

    duplicated = mongo_timeframes & mysql_timeframes
    report.duplicated_candle_timeframes = sorted(duplicated)

    for db in report.mongo_databases:
        for c in db.collection_stats:
            tf = _normalize_timeframe(c.name)
            is_dup = bool(tf and tf in duplicated)
            # System collections inside system DBs are not orphan-suspects
            if db.is_system:
                c.classification = "system"
            elif c.classification == "error":
                pass
            else:
                c.classification = _classify_mongo(c.name, is_dup)

    for schema in report.mysql_schemas:
        for t in schema.tables:
            tf = _normalize_timeframe(t.name)
            is_dup = bool(tf and tf in duplicated)
            if t.table_type == "VIEW":
                t.classification = "view"
            else:
                t.classification = _classify_mysql(t.name, is_dup)


def _attribute_400mb(report: StorageInventoryReport) -> None:
    """Aggregate on-disk storage by category for the >400 MB attribution.

    Categories (petrosa_k8s#794 AC3):
    - candles_*: per-symbol/per-timeframe Mongo collections (data-manager write path)
    - klines_logical: logical TS collection sizes
    - klines_buckets: backing system.buckets.klines_* (the real klines bytes)
    - other_app: app collections that are neither candle/kline nor system
    - oplog: local.oplog.rs
    - system_dbs: local + admin + config (excluding oplog double-counting)
    - mysql_klines: MySQL klines_* tables (excluding VIEWs)
    - mysql_other: MySQL non-klines base tables
    """
    candles = klines_logical = klines_buckets = other_app = 0
    system_dbs = 0
    oplog = int((report.mongo_oplog or {}).get("storageSize", 0) or 0)

    for db in report.mongo_databases:
        for c in db.collection_stats:
            if db.is_system:
                # local DB includes oplog.rs — but we report oplog separately
                if not (db.name == "local" and c.name == "oplog.rs"):
                    system_dbs += c.storage_size
                continue
            if c.name.startswith("candles_"):
                candles += c.storage_size
            elif c.name.startswith("klines_"):
                klines_logical += c.storage_size
                if c.backing_bucket_storage_size:
                    klines_buckets += c.backing_bucket_storage_size
            elif c.name.startswith("system.buckets.klines_"):
                klines_buckets += c.storage_size
            else:
                other_app += c.storage_size

    mysql_klines = 0
    mysql_other = 0
    for schema in report.mysql_schemas:
        for t in schema.tables:
            if t.table_type == "VIEW":
                continue
            total = t.data_length + t.index_length
            if t.name.startswith("klines_"):
                mysql_klines += total
            else:
                mysql_other += total

    report.attribution = {
        "mongo_candles_bytes": candles,
        "mongo_klines_logical_bytes": klines_logical,
        "mongo_klines_buckets_bytes": klines_buckets,
        "mongo_other_app_bytes": other_app,
        "mongo_oplog_bytes": oplog,
        "mongo_system_dbs_bytes": system_dbs,
        "mysql_klines_bytes": mysql_klines,
        "mysql_other_bytes": mysql_other,
    }


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def render_markdown(report: StorageInventoryReport) -> str:
    """Render a human-readable markdown summary of the audit."""
    lines: list[str] = []
    lines.append("# Storage Inventory Audit Report")
    lines.append("")
    lines.append(f"Generated: `{report.generated_at}`")
    lines.append("")
    lines.append(f"- Mongo backend: {'OK' if report.mongo_ok else 'FAILED'}")
    if report.mongo_error:
        lines.append(f"  - error: `{report.mongo_error}`")
    lines.append(f"- MySQL backend: {'OK' if report.mysql_ok else 'FAILED'}")
    if report.mysql_error:
        lines.append(f"  - error: `{report.mysql_error}`")
    lines.append("")

    if report.attribution:
        lines.append("## 400 MB Attribution (on-disk storageSize bytes)")
        lines.append("")
        lines.append("| Category | Bytes |")
        lines.append("| --- | ---: |")
        for k, v in report.attribution.items():
            lines.append(f"| `{k}` | `{v:,}` |")
        lines.append("")

    if report.duplicated_candle_timeframes:
        lines.append("## Duplicated candle timeframes (Mongo + MySQL)")
        lines.append("")
        for tf in report.duplicated_candle_timeframes:
            lines.append(f"- `{tf}`")
        lines.append("")

    if report.mongo_databases:
        lines.append("## MongoDB databases")
        lines.append("")
        for db in report.mongo_databases:
            lines.append(
                f"### `{db.name}` ({'system' if db.is_system else 'app'}) "
                f"— storageSize={db.storage_size:,} dataSize={db.data_size:,} "
                f"indexSize={db.index_size:,} collections={db.collections}"
            )
            if db.error:
                lines.append(f"_error_: `{db.error}`")
                lines.append("")
                continue
            lines.append("")
            lines.append(
                "| Collection | TS | storageSize | dataSize | indexSize | count | class |"
            )
            lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
            for c in db.collection_stats:
                tsmark = "TS" if c.is_timeseries else ""
                lines.append(
                    f"| `{c.name}` | {tsmark} | {c.storage_size:,} | {c.data_size:,} "
                    f"| {c.total_index_size:,} | {c.count:,} | {c.classification} |"
                )
            lines.append("")

    if report.mongo_oplog:
        lines.append(
            "## Oplog (`local.oplog.rs`) — "
            f"storageSize={report.mongo_oplog.get('storageSize', 0):,} "
            f"size={report.mongo_oplog.get('size', 0):,}"
        )
        lines.append("")

    if report.mysql_schemas:
        lines.append("## MySQL schemas")
        lines.append("")
        for schema in report.mysql_schemas:
            base_tables = [t for t in schema.tables if t.table_type != "VIEW"]
            views = [t for t in schema.tables if t.table_type == "VIEW"]
            base_total = sum(t.data_length + t.index_length for t in base_tables)
            lines.append(
                f"### `{schema.name}` — base-table bytes={base_total:,} "
                f"({len(base_tables)} tables / {len(views)} views)"
            )
            if schema.error:
                lines.append(f"_error_: `{schema.error}`")
                lines.append("")
                continue
            lines.append("")
            lines.append(
                "| Table | Type | DATA_LENGTH | INDEX_LENGTH | rows~ | class |"
            )
            lines.append("| --- | --- | ---: | ---: | ---: | --- |")
            for t in schema.tables:
                lines.append(
                    f"| `{t.name}` | {t.table_type} | {t.data_length:,} "
                    f"| {t.index_length:,} | {t.table_rows_estimated:,} "
                    f"| {t.classification} |"
                )
            lines.append("")

    return "\n".join(lines)


def report_to_dict(report: StorageInventoryReport) -> dict:
    """Convert the dataclass tree to a JSON-serializable dict."""
    return asdict(report)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.storage_inventory",
        description=(
            "Read-only audit of MongoDB + MySQL storage. Emits a markdown "
            "report + JSON blob to stdout. NEVER writes to either backend."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (in addition to markdown).",
    )
    parser.add_argument(
        "--mongo-only",
        action="store_true",
        help="Skip MySQL inventory.",
    )
    parser.add_argument(
        "--mysql-only",
        action="store_true",
        help="Skip MongoDB inventory.",
    )
    return parser


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _is_privilege_error(error: str | None) -> bool:
    if not error:
        return False
    lower = error.lower()
    return any(
        token in lower
        for token in (
            "not authorized",
            "permission denied",
            "access denied",
            "unauthorized",
        )
    )


async def _amain(argv: Iterable[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.mongo_only and args.mysql_only:
        logger.error("--mongo-only and --mysql-only are mutually exclusive")
        return 2

    run_mongo = not args.mysql_only
    run_mysql = not args.mongo_only

    mongo_url = os.getenv("MONGODB_URL")
    mysql_uri = os.getenv("MYSQL_URI")

    report = StorageInventoryReport(
        generated_at=datetime.utcnow().isoformat() + "Z",
        mongo_ok=False,
        mysql_ok=False,
        mongo_error=None,
        mysql_error=None,
    )

    if run_mongo:
        if not mongo_url:
            report.mongo_error = "MONGODB_URL not set"
        else:
            adapter = MongoDBAdapter(connection_string=mongo_url)
            adapter.connect()
            try:
                dbs, oplog, err = await audit_mongo(adapter)
                report.mongo_databases = dbs
                report.mongo_oplog = oplog
                report.mongo_error = err
                report.mongo_ok = err is None
            finally:
                adapter.disconnect()

    if run_mysql:
        if not mysql_uri:
            report.mysql_error = "MYSQL_URI not set"
        else:
            schemas, err = audit_mysql(mysql_uri)
            report.mysql_schemas = schemas
            report.mysql_error = err
            report.mysql_ok = err is None

    _classify_all(report)
    _attribute_400mb(report)

    print(render_markdown(report))
    if args.json:
        print("```json")
        print(json.dumps(report_to_dict(report), indent=2, default=str))
        print("```")

    return _compute_exit_code(report, run_mongo, run_mysql)


def _compute_exit_code(
    report: StorageInventoryReport, run_mongo: bool, run_mysql: bool
) -> int:
    """Map per-backend errors to the documented exit-code matrix (AC7)."""
    mongo_failed = run_mongo and not report.mongo_ok
    mysql_failed = run_mysql and not report.mysql_ok

    if mongo_failed and mysql_failed:
        if _is_privilege_error(report.mongo_error) or _is_privilege_error(
            report.mysql_error
        ):
            return EXIT_PRIVILEGE_DENIED
        return EXIT_BOTH_FAILED
    if mongo_failed:
        if _is_privilege_error(report.mongo_error):
            return EXIT_PRIVILEGE_DENIED
        return EXIT_MONGO_FAILED
    if mysql_failed:
        if _is_privilege_error(report.mysql_error):
            return EXIT_PRIVILEGE_DENIED
        return EXIT_MYSQL_FAILED
    return EXIT_OK


def main(argv: Iterable[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
