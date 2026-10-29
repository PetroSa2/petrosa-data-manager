#!/usr/bin/env python3
"""Compare isolated synthetic historic-storage workloads across SQL backends."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

SCHEMA = "benchmark_mysql_vs_postgres"
TABLES = ("klines", "signals", "positions", "execution_events")
WORKLOADS = (
    "bulk_ingest",
    "batched_upsert",
    "point_lookup",
    "time_range_lookup",
    "latest_per_symbol_lookup",
    "aggregate_statistical_query",
    "concurrent_reads",
    "concurrent_writes",
    "bounded_backfill_replay",
)


@dataclass(frozen=True)
class Settings:
    rows: int = 1_000
    warmups: int = 1
    repetitions: int = 3
    concurrency: int = 4
    batch_size: int = 250
    symbols: int = 10


def validate_settings(settings: Settings) -> None:
    if (
        min(settings.rows, settings.warmups, settings.repetitions, settings.concurrency)
        < 1
    ):
        raise ValueError("rows, warmups, repetitions, and concurrency must be positive")
    if settings.batch_size < 1 or settings.symbols < 1:
        raise ValueError("batch_size and symbols must be positive")


def validate_target(url: str, backend: str, schema: str = SCHEMA) -> None:
    parsed = urlparse(url)
    database = parsed.path.lstrip("/").split("/")[-1]
    if backend not in {"postgres", "mysql"}:
        raise ValueError("backend must be postgres or mysql")
    if not database:
        raise ValueError("benchmark URL must include a database")
    if schema != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    if not database.startswith("benchmark_") and not (
        backend == "postgres" and database == "psql_total"
    ):
        raise ValueError("target database is not an approved isolated benchmark target")
    if backend == "postgres":
        if parsed.hostname != "psql_total.postgresql.dbaas.com.br":
            raise ValueError(
                "PostgreSQL benchmark URL must target the approved benchmark provider"
            )
        query = parse_qs(parsed.query)
        if query.get("sslmode", [""])[0].lower() != "require":
            raise ValueError("PostgreSQL benchmark URL must set sslmode=require")
    if (
        parsed.hostname in {"localhost", "127.0.0.1"}
        and os.getenv("BENCHMARK_ALLOW_LOCAL") != "1"
    ):
        raise ValueError("local targets require BENCHMARK_ALLOW_LOCAL=1")


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://<redacted>@{host}{port}/{parsed.path.lstrip('/')}"


def synthetic_rows(settings: Settings) -> list[dict[str, Any]]:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        {
            "row_key": f"synthetic-{index}",
            "symbol": f"SYN{index % settings.symbols:02d}USD",
            "timeframe": "1m",
            "event_time": base + timedelta(minutes=index),
            "price": Decimal("100.0000") + Decimal(index % 100) / 100,
            "quantity": Decimal("1.0000") + Decimal(index % 20) / 10,
            "payload": {"source": "synthetic", "sequence": index},
            "is_open": index % 2 == 0,
        }
        for index in range(settings.rows)
    ]


def build_tables(metadata: MetaData, schema: str | None) -> dict[str, Table]:
    common = {"schema": schema}
    return {
        name: Table(
            name,
            metadata,
            Column("row_key", String(64), primary_key=True),
            Column("symbol", String(16), nullable=False),
            Column("timeframe", String(16), nullable=False),
            Column("event_time", DateTime(timezone=True), nullable=False),
            Column("price", Numeric(20, 8), nullable=False),
            Column("quantity", Numeric(20, 8), nullable=False),
            Column("payload", JSON(), nullable=False),
            Column("is_open", Boolean(), nullable=False),
            Index(f"ix_{name}_symbol_event_time", "symbol", "event_time"),
            **common,
        )
        for name in TABLES
    }


def _row(table: Table, value: dict[str, Any]) -> dict[str, Any]:
    columns = tuple(table.columns)
    return dict(zip(columns, value.values(), strict=True))


def create_schema(
    engine: Any, backend: str, settings: Settings
) -> tuple[MetaData, dict[str, Table]]:
    schema = SCHEMA if backend == "postgres" else None
    metadata = MetaData()
    tables = build_tables(metadata, schema)
    if backend == "postgres":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    metadata.drop_all(engine)
    metadata.create_all(engine)
    rows = synthetic_rows(settings)
    with engine.begin() as connection:
        for table in tables.values():
            connection.execute(table.insert(), [_row(table, row) for row in rows])
    return metadata, tables


def _upsert(connection: Any, table: Table, row: dict[str, Any], backend: str) -> None:
    values = _row(table, row)
    statement = (
        mysql_insert(table).values(values)
        if backend == "mysql"
        else postgres_insert(table).values(values)
    )
    statement = (
        statement.on_duplicate_key_update(**values)
        if backend == "mysql"
        else statement.on_conflict_do_update(
            index_elements=[table.c["row_key"]], set_=values
        )
    )
    connection.execute(statement)


def _workload(
    table: Table,
    name: str,
    rows: list[dict[str, Any]],
    backend: str,
    settings: Settings,
) -> Callable[[Any], None]:
    def run(connection: Any) -> None:
        if name == "bulk_ingest":
            connection.execute(
                table.insert(),
                [
                    _row(table, {**row, "row_key": f"bulk-{time.time_ns()}-{index}"})
                    for index, row in enumerate(rows)
                ],
            )
        elif name == "batched_upsert":
            for offset in range(
                0, min(settings.batch_size, len(rows)), settings.batch_size
            ):
                for row in rows[offset : offset + settings.batch_size]:
                    _upsert(connection, table, row, backend)
        elif name == "point_lookup":
            connection.execute(
                select(table).where(table.c.row_key == rows[0]["row_key"])
            ).first()
        elif name == "time_range_lookup":
            connection.execute(
                select(table).where(
                    table.c.event_time.between(
                        rows[0]["event_time"], rows[-1]["event_time"]
                    )
                )
            ).all()
        elif name == "latest_per_symbol_lookup":
            connection.execute(
                select(table.c.symbol, func.max(table.c.event_time)).group_by(
                    table.c.symbol
                )
            ).all()
        elif name == "aggregate_statistical_query":
            connection.execute(
                select(
                    func.count(), func.avg(table.c.price), func.sum(table.c.quantity)
                )
            ).one()
        elif name == "bounded_backfill_replay":
            for row in rows[: min(25, len(rows))]:
                _upsert(connection, table, row, backend)
        else:
            raise ValueError(f"unknown serial workload: {name}")

    return run


def measure(
    engine: Any,
    table: Table,
    name: str,
    rows: list[dict[str, Any]],
    backend: str,
    settings: Settings,
) -> dict[str, Any]:
    operation = _workload(table, name, rows, backend, settings)
    samples: list[float] = []
    errors = 0
    if name not in {"concurrent_reads", "concurrent_writes"}:
        for _ in range(settings.warmups):
            with engine.begin() as connection:
                operation(connection)
        for _ in range(settings.repetitions):
            started = time.perf_counter()
            try:
                with engine.begin() as connection:
                    operation(connection)
            except Exception:
                errors += 1
            samples.append((time.perf_counter() - started) * 1000)
    else:
        samples = []
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
            futures = [
                pool.submit(measure_one, engine, table, name, rows, backend)
                for _ in range(settings.concurrency)
            ]
            errors += sum(future.exception() is not None for future in futures)
            samples.extend(
                future.result() for future in futures if future.exception() is None
            )
        elapsed = (time.perf_counter() - started) * 1000
        throughput = settings.concurrency / max(elapsed / 1000, 0.000001)
    if name not in {"concurrent_reads", "concurrent_writes"}:
        throughput = settings.repetitions / max(sum(samples) / 1000, 0.000001)
    return {
        "workload": name,
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 3),
        "p99_ms": round(sorted(samples)[max(0, int(len(samples) * 0.99) - 1)], 3),
        "throughput_ops_s": round(throughput, 3),
        "errors": errors,
    }


def measure_one(
    engine: Any, table: Table, name: str, rows: list[dict[str, Any]], backend: str
) -> float:
    started = time.perf_counter()
    with engine.begin() as connection:
        if name == "concurrent_reads":
            connection.execute(
                select(table).where(table.c.row_key == rows[0]["row_key"])
            ).first()
        else:
            _upsert(connection, table, rows[0], backend)
    return (time.perf_counter() - started) * 1000


def run_target(url: str, backend: str, settings: Settings) -> dict[str, Any]:
    validate_target(url, backend)
    driver = "postgresql+psycopg" if backend == "postgres" else "mysql+pymysql"
    engine = create_engine(
        url.replace("postgresql://", f"{driver}://").replace(
            "mysql://", f"{driver}://"
        ),
        pool_pre_ping=True,
    )
    _, tables = create_schema(engine, backend, settings)
    rows = synthetic_rows(settings)
    results = [
        {"table": table_name, **measure(engine, table, name, rows, backend, settings)}
        for table_name, table in tables.items()
        for name in WORKLOADS
    ]
    return {
        "backend": backend,
        "target": redact_url(url),
        "schema": SCHEMA,
        "dataset": {
            "rows_per_table": settings.rows,
            "tables": list(TABLES),
            "synthetic": True,
        },
        "parameters": settings.__dict__,
        "results": results,
        "pool": {"size": getattr(engine.pool, "size", lambda: None)()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rows", type=int, default=Settings.rows)
    args = parser.parse_args()
    settings = Settings(rows=args.rows)
    validate_settings(settings)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "synthetic": True,
                    "workloads": WORKLOADS,
                    "parameters": settings.__dict__,
                }
            )
        )
        return
    urls = {
        "postgres": os.environ["BENCHMARK_POSTGRES_URL"],
        "mysql": os.environ["BENCHMARK_MYSQL_URL"],
    }
    print(
        json.dumps(
            {
                "started_at": datetime.now(UTC).isoformat(),
                "targets": [
                    run_target(url, backend, settings) for backend, url in urls.items()
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
