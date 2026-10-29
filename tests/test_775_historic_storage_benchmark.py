from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from scripts.historic_storage_benchmark import (
    SCHEMA,
    WORKLOADS,
    Settings,
    _row,
    _upsert,
    _workload,
    build_tables,
    create_schema,
    main,
    measure,
    measure_one,
    redact_url,
    run_target,
    synthetic_rows,
    validate_settings,
    validate_target,
)


def test_dry_run_contract_is_synthetic_and_complete():
    assert len(WORKLOADS) == 9
    assert {"concurrent_reads", "concurrent_writes", "bounded_backfill_replay"} <= set(
        WORKLOADS
    )
    assert synthetic_rows(Settings(rows=2))[0]["payload"] == {
        "source": "synthetic",
        "sequence": 0,
    }


def test_target_validation_rejects_production_and_missing_tls():
    with pytest.raises(ValueError, match="approved isolated") as production_error:
        validate_target("mysql+pymysql://user:pass@db/petrosa_crypto", "mysql")
    with pytest.raises(ValueError, match="sslmode=require") as tls_error:
        validate_target(
            "postgresql://user:pass@psql_total.postgresql.dbaas.com.br/psql_total",
            "postgres",
        )
    assert production_error.value.args and tls_error.value.args


def test_target_validation_allows_only_explicit_isolated_targets():
    validate_target(
        "postgresql://user:pass@psql_total.postgresql.dbaas.com.br/psql_total?sslmode=require",
        "postgres",
    )
    validate_target("mysql+pymysql://user:pass@db/benchmark_mysql_vs_postgres", "mysql")
    assert redact_url(
        "postgresql://secret:password@psql_total.postgresql.dbaas.com.br:5432/psql_total?sslmode=require"
    ) == ("postgresql://<redacted>@psql_total.postgresql.dbaas.com.br:5432/psql_total")


def test_settings_are_bounded_and_schema_is_fixed():
    validate_settings(Settings(rows=10, warmups=1, repetitions=1, concurrency=2))
    assert SCHEMA == "benchmark_mysql_vs_postgres"
    with pytest.raises(ValueError):
        validate_settings(Settings(rows=0))


def test_sqlite_schema_and_serial_workloads(monkeypatch):
    settings = Settings(rows=4, warmups=1, repetitions=1, batch_size=2, concurrency=2)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _, tables = create_schema(engine, "mysql", settings)
    rows = synthetic_rows(settings)
    assert set(tables) == {"klines", "signals", "positions", "execution_events"}
    assert _row(tables["klines"], rows[0])["row_key"] == "synthetic-0"
    for name in (
        "point_lookup",
        "time_range_lookup",
        "latest_per_symbol_lookup",
        "aggregate_statistical_query",
    ):
        result = measure(engine, tables["klines"], name, rows, "mysql", settings)
        assert result["errors"] == 0
    bulk = measure(engine, tables["klines"], "bulk_ingest", rows, "mysql", settings)
    assert bulk["throughput_ops_s"] > 0
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark._upsert",
        lambda connection, table, row, backend: None,
    )
    replay = measure(
        engine, tables["klines"], "bounded_backfill_replay", rows, "mysql", settings
    )
    assert replay["errors"] == 0


class _RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)


def test_upsert_and_concurrent_workloads(monkeypatch):
    settings = Settings(rows=2, warmups=1, repetitions=1, concurrency=2)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _, tables = create_schema(engine, "mysql", settings)
    connection = _RecordingConnection()
    _upsert(connection, tables["klines"], synthetic_rows(settings)[0], "mysql")
    assert len(connection.statements) == 1
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark._upsert",
        lambda connection, table, row, backend: None,
    )
    rows = synthetic_rows(settings)
    assert (
        measure(engine, tables["klines"], "concurrent_reads", rows, "mysql", settings)[
            "errors"
        ]
        == 0
    )
    assert (
        measure(engine, tables["klines"], "concurrent_writes", rows, "mysql", settings)[
            "errors"
        ]
        == 0
    )
    with pytest.raises(ValueError, match="unknown serial"):
        _workload(tables["klines"], "unknown", rows, "mysql", settings)(connection)
    assert measure_one(engine, tables["klines"], "concurrent_reads", rows, "mysql") >= 0


def test_run_target_and_dry_run(monkeypatch, capsys):
    settings = Settings(rows=1)
    engine = SimpleNamespace(pool=SimpleNamespace(size=lambda: 1))
    tables = {"klines": object()}
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.create_engine",
        lambda *args, **kwargs: engine,
    )
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.create_schema",
        lambda actual_engine, backend, actual_settings: (None, tables),
    )
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.measure",
        lambda *args: {"workload": args[2], "errors": 0},
    )
    result = run_target(
        "postgresql://u:p@psql_total.postgresql.dbaas.com.br/psql_total?sslmode=require",
        "postgres",
        settings,
    )
    assert result["dataset"]["synthetic"] is True
    monkeypatch.setattr("sys.argv", ["benchmark", "--dry-run"])
    main()
    assert '"synthetic": true' in capsys.readouterr().out
