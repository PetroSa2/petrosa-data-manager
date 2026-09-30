"""Capture Percona userstat counters for hourly index-usage trending.

The job deliberately uses the read-only SQLAlchemy engine helper and stores one
MongoDB document per run.  The absolute OTLP gauges are a companion signal;
MongoDB is the durable source for reset-aware comparisons.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from opentelemetry import metrics

from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import create_read_only_engine

try:
    from petrosa_otel import get_meter
except ImportError:  # pragma: no cover
    get_meter = metrics.get_meter

logger = logging.getLogger(__name__)

COLLECTION = "mysql_index_usage_snapshots"
TTL_INDEX_NAME = "captured_at_ttl_90d"
DEFAULT_SCHEMA = "petrosa_crypto"
DEFAULT_TTL_SECONDS = 90 * 24 * 60 * 60
EXIT_OK = 0
EXIT_USERSTAT_UNAVAILABLE = 11

_meter = get_meter(__name__)
_index_rows_read = _meter.create_gauge(
    "data_manager_mysql_index_rows_read", description="Absolute index rows read"
)
_table_rows_read = _meter.create_gauge(
    "data_manager_mysql_table_rows_read", description="Absolute table rows read"
)
_table_rows_changed = _meter.create_gauge(
    "data_manager_mysql_table_rows_changed", description="Absolute table rows changed"
)


def _rows(result: Any) -> list[dict[str, Any]]:
    """Normalize SQLAlchemy rows and simple test doubles."""
    mappings = result.mappings() if hasattr(result, "mappings") else result
    values = mappings.fetchall() if hasattr(mappings, "fetchall") else mappings
    return [dict(row) for row in values]


def read_statistics(engine: Any, schema: str) -> tuple[list[dict], list[dict], int]:
    """Read the two userstat views and server uptime in three statements."""
    index_stmt = sa.text(
        "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, ROWS_READ "
        "FROM INFORMATION_SCHEMA.INDEX_STATISTICS WHERE TABLE_SCHEMA = :schema"
    )
    table_stmt = sa.text(
        "SELECT TABLE_SCHEMA, TABLE_NAME, ROWS_READ, ROWS_CHANGED, "
        "ROWS_CHANGED_X_INDEXES FROM INFORMATION_SCHEMA.TABLE_STATISTICS "
        "WHERE TABLE_SCHEMA = :schema"
    )
    uptime_stmt = sa.text("SHOW GLOBAL STATUS LIKE 'Uptime'")
    with engine.connect() as connection:
        indexes = _rows(connection.execute(index_stmt, {"schema": schema}))
        tables = _rows(connection.execute(table_stmt, {"schema": schema}))
        uptime_rows = _rows(connection.execute(uptime_stmt))
    uptime = int(uptime_rows[0].get("Value", uptime_rows[0].get("VALUE", 0)))
    return indexes, tables, uptime


def _index_documents(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "table": row["TABLE_NAME"],
            "index": row["INDEX_NAME"],
            "rows_read": int(row.get("ROWS_READ") or 0),
        }
        for row in rows
    ]


def _table_documents(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "table": row["TABLE_NAME"],
            "rows_read": int(row.get("ROWS_READ") or 0),
            "rows_changed": int(row.get("ROWS_CHANGED") or 0),
            "rows_changed_x_indexes": int(row.get("ROWS_CHANGED_X_INDEXES") or 0),
        }
        for row in rows
    ]


def counter_delta(previous_snapshot: dict, current_snapshot: dict) -> dict | None:
    """Return comparable index deltas, or ``None`` when counters reset."""
    if (
        current_snapshot["server_uptime_seconds"]
        < previous_snapshot["server_uptime_seconds"]
    ):
        return None
    previous = {
        (row["table"], row["index"]): row["rows_read"]
        for row in previous_snapshot.get("indexes", [])
    }
    current = {
        (row["table"], row["index"]): row["rows_read"]
        for row in current_snapshot.get("indexes", [])
    }
    if any(current[key] < value for key, value in previous.items() if key in current):
        return None
    return {
        "indexes": {
            f"{table}.{index}": current[(table, index)] - value
            for (table, index), value in previous.items()
            if (table, index) in current
        },
        "dropped": [
            f"{table}.{index}"
            for table, index in previous
            if (table, index) not in current
        ],
    }


def _emit_metrics(indexes: list[dict], tables: list[dict]) -> None:
    for row in indexes:
        _index_rows_read.set(
            row["rows_read"], {"table": row["table"], "index": row["index"]}
        )
    for row in tables:
        attrs = {"table": row["table"]}
        _table_rows_read.set(row["rows_read"], attrs)
        _table_rows_changed.set(row["rows_changed"], attrs)


def flush_metrics() -> None:
    provider = metrics.get_meter_provider()
    flush = getattr(provider, "force_flush", None)
    if callable(flush):
        try:
            flush()
        except Exception:  # noqa: BLE001
            logger.warning("OTLP metric flush failed", exc_info=True)


async def run_snapshot(
    engine: Any,
    mongo: MongoDBAdapter,
    *,
    schema: str = DEFAULT_SCHEMA,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    dry_run: bool = False,
    captured_at: datetime | None = None,
) -> dict:
    """Read, emit, and optionally persist one snapshot."""
    indexes, tables, uptime = read_statistics(engine, schema)
    captured = captured_at or datetime.now(UTC)
    document = {
        "captured_at": captured,
        "schema": schema,
        "server_uptime_seconds": uptime,
        "indexes": _index_documents(indexes),
        "tables": _table_documents(tables),
    }
    _emit_metrics(document["indexes"], document["tables"])
    logger.info(
        "index_usage_snapshot: indexes=%d tables=%d captured_at=%s",
        len(indexes),
        len(tables),
        captured.isoformat(),
    )
    if not dry_run:
        collection = mongo.db[COLLECTION]
        await collection.create_index(
            [("captured_at", 1)], name=TTL_INDEX_NAME, expireAfterSeconds=ttl_seconds
        )
        await collection.insert_one(document)
    return document


def load_config_from_env(environ: dict[str, str] | None = None) -> tuple[str, int]:
    env = environ if environ is not None else os.environ
    schema = env.get("MYSQL_SCHEMA", DEFAULT_SCHEMA)
    try:
        ttl = max(
            60, int(env.get("INDEX_USAGE_SNAPSHOT_TTL_SECONDS", DEFAULT_TTL_SECONDS))
        )
    except ValueError:
        ttl = DEFAULT_TTL_SECONDS
    return schema, ttl


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.index_usage_snapshot"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _build_argparser().parse_args(argv)
    schema, ttl = load_config_from_env()
    mysql_url = os.getenv("MYSQL_URI") or os.getenv("MYSQL_URL")
    mongo_url = os.getenv("MONGODB_URL")
    if not mysql_url or not mongo_url:
        logger.error("MYSQL_URI/MYSQL_URL and MONGODB_URL are required")
        return EXIT_USERSTAT_UNAVAILABLE
    engine = None
    mongo = None
    try:
        engine = create_read_only_engine(mysql_url)
        mongo = MongoDBAdapter(connection_string=mongo_url)
        mongo.connect()
        document = asyncio.run(
            run_snapshot(
                engine, mongo, schema=schema, ttl_seconds=ttl, dry_run=args.dry_run
            )
        )
        if args.json:
            logger.info(
                "index_usage_snapshot_report %s",
                json.dumps({"level": "INFO", **document}, default=str),
            )
        flush_metrics()
        return EXIT_OK
    except Exception as exc:  # userstat is optional and may be absent
        logger.warning("index_usage_snapshot unavailable; no snapshot written: %s", exc)
        return EXIT_USERSTAT_UNAVAILABLE
    finally:
        if mongo is not None:
            mongo.disconnect()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
