#!/usr/bin/env python3
"""Compare isolated synthetic historic-storage workloads across SQL backends."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
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
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

SCHEMA = "benchmark_mysql_vs_postgres"
TABLES = ("klines", "signals", "positions", "execution_events")
DEFAULT_MYSQL_TABLE_PREFIX = "bench775_"
MYSQL_CONNECTION_CAP = 4
STALL_TIMEOUT_SECONDS = 60.0
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
    statement_timeout_ms: int = 30_000
    phase_budget_seconds: float = 300.0
    mysql_table_prefix: str = DEFAULT_MYSQL_TABLE_PREFIX


def validate_settings(settings: Settings) -> None:
    if (
        min(settings.rows, settings.warmups, settings.repetitions, settings.concurrency)
        < 1
    ):
        raise ValueError("rows, warmups, repetitions, and concurrency must be positive")
    if settings.batch_size < 1 or settings.symbols < 1:
        raise ValueError("batch_size and symbols must be positive")
    if settings.statement_timeout_ms < 1 or settings.phase_budget_seconds <= 0:
        raise ValueError("timeouts must be positive")
    validate_mysql_table_prefix(settings.mysql_table_prefix)


def validate_mysql_table_prefix(prefix: str) -> None:
    if not prefix or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prefix):
        raise ValueError("mysql table prefix must be a non-empty SQL identifier prefix")


def validate_target(
    url: str,
    backend: str,
    schema: str = SCHEMA,
    mysql_table_prefix: str = DEFAULT_MYSQL_TABLE_PREFIX,
) -> None:
    parsed = urlparse(url)
    database = parsed.path.lstrip("/").split("/")[-1]
    if backend not in {"postgres", "mysql"}:
        raise ValueError("backend must be postgres or mysql")
    if not database:
        raise ValueError("benchmark URL must include a database")
    if schema != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    if backend == "mysql":
        validate_mysql_table_prefix(mysql_table_prefix)
    elif database != "psql_total":
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


def _redact_error(error: Exception, url: str) -> str:
    message = str(error).replace(url, redact_url(url))
    return message.replace("password", "[redacted]")


def _engine_url(url: str, backend: str) -> str:
    driver = "postgresql+psycopg" if backend == "postgres" else "mysql+pymysql"
    return url.replace("postgresql://", f"{driver}://").replace(
        "mysql://", f"{driver}://"
    )


def _connect_args(backend: str) -> dict[str, Any]:
    args: dict[str, Any] = {"connect_timeout": 10}
    if backend == "mysql":
        args["init_command"] = "SET SESSION innodb_lock_wait_timeout = 30"
    return args


def _mysql_connect_hook(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION innodb_lock_wait_timeout = 30")
        cursor.execute("SET SESSION lock_wait_timeout = 30")
    finally:
        cursor.close()


def _install_connect_hooks(engine: Any, backend: str) -> None:
    if backend == "mysql" and hasattr(engine, "connect"):
        event.listen(engine, "connect", _mysql_connect_hook)


def _engine_kwargs(backend: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "connect_args": _connect_args(backend),
        "pool_pre_ping": True,
    }
    if backend == "mysql":
        kwargs.update(pool_size=MYSQL_CONNECTION_CAP, max_overflow=0)
    return kwargs


def preflight_target(
    url: str,
    backend: str,
    emit: Callable[[str], None] = print,
    engine: Any | None = None,
    mysql_table_prefix: str = DEFAULT_MYSQL_TABLE_PREFIX,
) -> bool:
    """Verify connectivity and isolated-schema DDL before any benchmark writes."""
    try:
        validate_target(url, backend, mysql_table_prefix=mysql_table_prefix)
        if engine is None:
            engine = create_engine(_engine_url(url, backend), **_engine_kwargs(backend))
            _install_connect_hooks(engine, backend)
        table = (
            f"{mysql_table_prefix}benchmark_preflight_probe"
            if backend == "mysql"
            else "benchmark_preflight_probe"
        )
        qualified = f'"{SCHEMA}"."{table}"' if backend == "postgres" else f"`{table}`"
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
            if backend == "postgres":
                connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
            connection.execute(text(f"DROP TABLE IF EXISTS {qualified}"))
            connection.execute(text(f"CREATE TABLE {qualified} (probe_id INTEGER)"))
            connection.execute(text(f"DROP TABLE {qualified}"))
        emit(f"PREFLIGHT {backend} ok")
        return True
    except Exception as error:
        emit(
            f"PREFLIGHT {backend} fail {type(error).__name__}: {_redact_error(error, url)}"
        )
        return False


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


def build_tables(
    metadata: MetaData,
    schema: str | None,
    mysql_table_prefix: str = DEFAULT_MYSQL_TABLE_PREFIX,
) -> dict[str, Table]:
    validate_mysql_table_prefix(mysql_table_prefix)
    common = {"schema": schema}
    return {
        name: Table(
            f"{mysql_table_prefix}{name}" if schema is None else name,
            metadata,
            Column("row_key", String(64), primary_key=True),
            Column("symbol", String(16), nullable=False),
            Column("timeframe", String(16), nullable=False),
            Column("event_time", DateTime(timezone=True), nullable=False),
            Column("price", Numeric(20, 8), nullable=False),
            Column("quantity", Numeric(20, 8), nullable=False),
            Column("payload", JSON(), nullable=False),
            Column("is_open", Boolean(), nullable=False),
            Index(
                f"ix_{mysql_table_prefix}{name}_symbol_event_time"
                if schema is None
                else f"ix_{name}_symbol_event_time",
                "symbol",
                "event_time",
            ),
            **common,
        )
        for name in TABLES
    }


def _row(table: Table, value: dict[str, Any]) -> dict[str, Any]:
    columns = tuple(table.columns)
    return dict(zip((column.name for column in columns), value.values(), strict=True))


def create_schema(
    engine: Any,
    backend: str,
    settings: Settings,
) -> tuple[MetaData, dict[str, Table]]:
    schema = SCHEMA if backend == "postgres" else None
    metadata = MetaData()
    tables = build_tables(metadata, schema, settings.mysql_table_prefix)
    if backend == "postgres":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    if backend == "mysql":
        with engine.begin() as connection:
            for table in tables.values():
                connection.execute(text(f"DROP TABLE IF EXISTS `{table.name}`"))
    else:
        metadata.drop_all(engine)
    metadata.create_all(engine)
    rows = synthetic_rows(settings)
    with engine.begin() as connection:
        for table in tables.values():
            connection.execute(table.insert(), [_row(table, row) for row in rows])
    return metadata, tables


def cleanup_schema(engine: Any, backend: str, tables: dict[str, Table]) -> None:
    if backend == "mysql":
        with engine.begin() as connection:
            for table in tables.values():
                connection.execute(text(f"DROP TABLE IF EXISTS `{table.name}`"))
    else:
        metadata = MetaData()
        for table in tables.values():
            table.to_metadata(metadata)
        metadata.drop_all(engine)


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
    progress: Callable[[int], None] | None = None,
) -> Callable[[Any], int]:
    def run(connection: Any) -> int:
        if name == "bulk_ingest":
            for offset in range(0, len(rows), settings.batch_size):
                batch = rows[offset : offset + settings.batch_size]
                connection.execute(
                    table.insert(),
                    [
                        _row(
                            table, {**row, "row_key": f"bulk-{time.time_ns()}-{index}"}
                        )
                        for index, row in enumerate(batch, offset)
                    ],
                )
                if progress:
                    progress(min(offset + len(batch), len(rows)))
            return len(rows)
        elif name == "batched_upsert":
            for offset in range(
                0, min(settings.batch_size, len(rows)), settings.batch_size
            ):
                for row in rows[offset : offset + settings.batch_size]:
                    _upsert(connection, table, row, backend)
            return min(settings.batch_size, len(rows))
        elif name == "point_lookup":
            connection.execute(
                select(table).where(table.c.row_key == rows[0]["row_key"])
            ).first()
            return 1
        elif name == "time_range_lookup":
            result = connection.execute(
                select(table).where(
                    table.c.symbol == rows[0]["symbol"],
                    table.c.timeframe == rows[0]["timeframe"],
                    table.c.event_time.between(
                        rows[0]["event_time"], rows[-1]["event_time"]
                    ),
                )
            ).all()
            return len(result)
        elif name == "latest_per_symbol_lookup":
            connection.execute(
                select(table.c.symbol, func.max(table.c.event_time)).group_by(
                    table.c.symbol
                )
            ).all()
            return len(rows)
        elif name == "aggregate_statistical_query":
            connection.execute(
                select(
                    func.count(), func.avg(table.c.price), func.sum(table.c.quantity)
                )
            ).one()
            return 1
        elif name == "bounded_backfill_replay":
            for row in rows[: min(25, len(rows))]:
                _upsert(connection, table, row, backend)
            return min(25, len(rows))
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
    started_phase = time.perf_counter()
    last_report = 0.0
    last_progress = started_phase
    last_rows = 0
    watchdog_reported = False
    stop_watchdog = threading.Event()

    def report(rows_done: int) -> None:
        nonlocal last_progress, last_report, last_rows
        last_rows = rows_done
        now = time.perf_counter()
        last_progress = now
        elapsed = now - started_phase
        if elapsed - (last_report - started_phase) >= 30 or rows_done == len(rows):
            print(f"PROGRESS {backend} {name} rows={rows_done} elapsed={elapsed:.1f}s")
            last_report = now

    def watchdog() -> None:
        nonlocal watchdog_reported
        poll_interval = min(1.0, max(STALL_TIMEOUT_SECONDS / 4, 0.01))
        while not stop_watchdog.wait(poll_interval):
            elapsed = time.perf_counter() - started_phase
            if (
                time.perf_counter() - last_progress >= STALL_TIMEOUT_SECONDS
                and not watchdog_reported
            ):
                watchdog_reported = True
                print(f"STALL {backend} {name} rows={last_rows} elapsed={elapsed:.1f}s")
                if backend == "mysql":
                    _print_mysql_processlist(engine)
                return

    print(f"PROGRESS {backend} {name} rows=0 elapsed=0.0s")
    operation = _workload(table, name, rows, backend, settings, report)
    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()

    rows_returned = None

    def execute_phase() -> int:
        started = time.perf_counter()
        with engine.begin() as connection:
            _set_statement_timeout(connection, backend, settings.statement_timeout_ms)
            completed = operation(connection)
        if time.perf_counter() - started > settings.phase_budget_seconds:
            raise TimeoutError(f"{backend} {name} exceeded phase budget")
        return completed

    samples: list[float] = []
    errors = 0
    completed_operations = 0
    status = "ok"
    error_class = None
    if name not in {"concurrent_reads", "concurrent_writes"}:
        for _ in range(settings.warmups):
            try:
                execute_phase()
            except Exception as error:
                status = "timeout" if isinstance(error, TimeoutError) else "error"
                error_class = type(error).__name__
                errors += 1
                break
        for _ in range(settings.repetitions):
            started = time.perf_counter()
            try:
                phase_completed = execute_phase()
                completed_operations += 1
                if name == "time_range_lookup":
                    rows_returned = phase_completed
            except Exception as error:
                errors += 1
                status = "timeout" if isinstance(error, TimeoutError) else "error"
                error_class = type(error).__name__
            samples.append((time.perf_counter() - started) * 1000)
    else:
        samples = []
        started = time.perf_counter()
        worker_count = min(
            settings.concurrency,
            MYSQL_CONNECTION_CAP - 1 if backend == "mysql" else settings.concurrency,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(measure_one, engine, table, name, rows, backend, settings)
                for _ in range(worker_count)
            ]
            errors += sum(future.exception() is not None for future in futures)
            samples.extend(
                future.result() for future in futures if future.exception() is None
            )
            completed_operations = len(samples)
            if errors:
                status = "error"
                error_class = "WorkloadError"
        elapsed = (time.perf_counter() - started) * 1000
        throughput = worker_count / max(elapsed / 1000, 0.000001)
    if name not in {"concurrent_reads", "concurrent_writes"}:
        throughput = settings.repetitions / max(sum(samples) / 1000, 0.000001)
    stop_watchdog.set()
    watchdog_thread.join(timeout=1.0)
    elapsed = time.perf_counter() - started_phase
    print(f"PROGRESS {backend} {name} rows={len(rows)} elapsed={elapsed:.1f}s")
    result = {
        "workload": name,
        "status": status,
        "error_class": error_class,
        "completed_operations": completed_operations,
        "rows_returned": rows_returned,
        "elapsed_ms": round(elapsed * 1000, 3),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 3),
        "p99_ms": round(sorted(samples)[max(0, int(len(samples) * 0.99) - 1)], 3),
        "throughput_ops_s": round(throughput, 3),
        "errors": errors,
    }
    return result


def _print_mysql_processlist(engine: Any) -> None:
    try:
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT ID, TIME, STATE, LEFT(INFO,120) "
                    "FROM information_schema.PROCESSLIST WHERE DB = DATABASE() LIMIT 20"
                )
            ).all()
        for row in rows:
            values = [_redact_process_value(value) for value in row]
            print(f"PROCESS mysql [{', '.join(values)}]")
    except Exception as error:
        print(f"PROCESS mysql diagnostics_error={type(error).__name__}")


def _redact_process_value(value: Any) -> str:
    text_value = str(value).replace("\n", " ")[:120]
    text_value = re.sub(r"://[^\s/@]+:[^\s/@]+@", "://[redacted]@", text_value)
    return re.sub(r"(?i)password\s*[=:]\s*[^\s,]+", "password=[redacted]", text_value)


def measure_one(
    engine: Any,
    table: Table,
    name: str,
    rows: list[dict[str, Any]],
    backend: str,
    settings: Settings | None = None,
) -> float:
    started = time.perf_counter()
    with engine.begin() as connection:
        if settings:
            _set_statement_timeout(connection, backend, settings.statement_timeout_ms)
        if name == "concurrent_reads":
            connection.execute(
                select(table).where(table.c.row_key == rows[0]["row_key"])
            ).first()
        else:
            _upsert(connection, table, rows[0], backend)
    return (time.perf_counter() - started) * 1000


def _set_statement_timeout(connection: Any, backend: str, timeout_ms: int) -> None:
    if getattr(getattr(connection, "dialect", None), "name", None) == "sqlite":
        return
    if backend == "postgres":
        connection.execute(text(f"SET statement_timeout = {timeout_ms}"))
    elif backend == "mysql":
        connection.execute(text(f"SET SESSION max_execution_time = {timeout_ms}"))


def measure_rtt(
    engine: Any, settings: Settings, backend: str = "benchmark"
) -> dict[str, Any]:
    samples: list[float] = []
    error_class = None
    for _ in range(50):
        started = time.perf_counter()
        try:
            with engine.begin() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as error:
            error_class = type(error).__name__
            break
        samples.append((time.perf_counter() - started) * 1000)
    if not samples:
        return {"status": "error", "error_class": error_class, "samples": 0}
    ordered = sorted(samples)
    result = {
        "status": "ok",
        "samples": len(samples),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
    }
    print(
        f"PROGRESS {backend} rtt p50={result['p50_ms']:.3f} p95={result['p95_ms']:.3f}"
    )
    return result


def run_target(url: str, backend: str, settings: Settings) -> dict[str, Any]:
    validate_target(url, backend, mysql_table_prefix=settings.mysql_table_prefix)
    engine = create_engine(_engine_url(url, backend), **_engine_kwargs(backend))
    _install_connect_hooks(engine, backend)
    if not preflight_target(
        url,
        backend,
        engine=engine,
        mysql_table_prefix=settings.mysql_table_prefix,
    ):
        raise RuntimeError(f"{backend} preflight failed")
    rtt_baseline = measure_rtt(engine, settings, backend)
    _, tables = create_schema(engine, backend, settings)
    try:
        rows = synthetic_rows(settings)
        results = []
        for name in WORKLOADS:
            for table_name, table in tables.items():
                try:
                    measured = measure(engine, table, name, rows, backend, settings)
                except Exception as error:
                    measured = {
                        "workload": name,
                        "status": "timeout"
                        if isinstance(error, TimeoutError)
                        else "error",
                        "error_class": type(error).__name__,
                        "completed_operations": 0,
                        "elapsed_ms": 0.0,
                        "errors": 1,
                    }
                results.append({"table": table_name, **measured})
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
            "rtt_baseline": rtt_baseline,
            "results": results,
            "pool": {
                "size": getattr(engine.pool, "size", lambda: None)(),
                "max_overflow": getattr(engine.pool, "_max_overflow", None),
            },
        }
    finally:
        cleanup_schema(engine, backend, tables)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows", type=int, default=Settings.rows)
    parser.add_argument("--warmups", type=int, default=Settings.warmups)
    parser.add_argument("--repetitions", type=int, default=Settings.repetitions)
    parser.add_argument("--concurrency", type=int, default=Settings.concurrency)
    parser.add_argument("--batch-size", type=int, default=Settings.batch_size)
    parser.add_argument("--targets", default="postgres,mysql")
    parser.add_argument("--mysql-table-prefix", default=DEFAULT_MYSQL_TABLE_PREFIX)
    args = parser.parse_args()
    selected_targets = tuple(target.strip() for target in args.targets.split(","))
    if (
        not selected_targets
        or any(target not in {"postgres", "mysql"} for target in selected_targets)
        or len(set(selected_targets)) != len(selected_targets)
    ):
        parser.error("--targets must be a comma-separated list of postgres,mysql")
    settings = Settings(
        rows=args.rows,
        warmups=args.warmups,
        repetitions=args.repetitions,
        concurrency=args.concurrency,
        batch_size=args.batch_size,
        mysql_table_prefix=args.mysql_table_prefix,
    )
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
        backend: os.environ[f"BENCHMARK_{backend.upper()}_URL"]
        for backend in selected_targets
    }
    if args.preflight:
        results = [
            preflight_target(
                url,
                backend,
                mysql_table_prefix=settings.mysql_table_prefix,
            )
            for backend, url in urls.items()
        ]
        if not all(results):
            raise SystemExit(1)
        return
    targets = []
    for backend, url in urls.items():
        try:
            targets.append(run_target(url, backend, settings))
        except Exception as error:
            targets.append(
                {
                    "backend": backend,
                    "target": redact_url(url),
                    "schema": SCHEMA,
                    "results": [],
                    "status": "timeout" if isinstance(error, TimeoutError) else "error",
                    "error_class": type(error).__name__,
                    "completed_operations": 0,
                    "elapsed_ms": 0.0,
                }
            )
    result = {
        "started_at": datetime.now(UTC).isoformat(),
        "targets": targets,
    }
    output = json.dumps(result)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
