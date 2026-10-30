from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from scripts.historic_storage_benchmark import (
    DEFAULT_MYSQL_TABLE_PREFIX,
    SCHEMA,
    WORKLOADS,
    Settings,
    _row,
    _upsert,
    _workload,
    build_tables,
    cleanup_schema,
    create_schema,
    main,
    measure,
    measure_one,
    preflight_target,
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


def test_target_validation_rejects_empty_prefix_and_missing_tls():
    with pytest.raises(ValueError, match="non-empty") as empty_prefix_error:
        validate_target(
            "mysql+pymysql://user:pass@db/petrosa_crypto",
            "mysql",
            mysql_table_prefix="",
        )
    with pytest.raises(ValueError, match="sslmode=require") as tls_error:
        validate_target(
            "postgresql://user:pass@psql_total.postgresql.dbaas.com.br/psql_total",
            "postgres",
        )
    assert empty_prefix_error.value.args and tls_error.value.args


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
    assert Settings().mysql_table_prefix == DEFAULT_MYSQL_TABLE_PREFIX
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
        "scripts.historic_storage_benchmark.preflight_target",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.cleanup_schema",
        lambda *args, **kwargs: None,
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


def test_mysql_engine_connection_cap(monkeypatch):
    settings = Settings(rows=1)
    engine = SimpleNamespace(pool=SimpleNamespace(size=lambda: 4))
    captured = {}
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.create_engine",
        lambda *args, **kwargs: captured.update(kwargs) or engine,
    )
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.preflight_target",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.create_schema",
        lambda *args, **kwargs: (None, {}),
    )
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.cleanup_schema",
        lambda *args, **kwargs: None,
    )
    result = run_target("mysql+pymysql://u:p@db/petrosa_crypto", "mysql", settings)
    assert captured["pool_size"] == 4
    assert captured["max_overflow"] == 0
    assert result["pool"]["size"] == 4


class _PreflightConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, error=None):
        self.error = error
        self.statements = []

    def execute(self, statement):
        if self.error:
            raise self.error
        self.statements.append(str(statement))


class _PreflightEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        class Context:
            def __enter__(inner):
                return self.connection

            def __exit__(inner, *args):
                return False

        return Context()


def test_preflight_reports_success_and_redacted_failure():
    url = "postgresql://user:secret@psql_total.postgresql.dbaas.com.br/psql_total?sslmode=require"
    lines = []
    assert preflight_target(
        url, "postgres", lines.append, _PreflightEngine(_PreflightConnection())
    )
    assert lines == ["PREFLIGHT postgres ok"]
    lines.clear()
    error = RuntimeError(f"could not connect to {url}")
    assert not preflight_target(
        url, "postgres", lines.append, _PreflightEngine(_PreflightConnection(error))
    )
    assert "PREFLIGHT postgres fail RuntimeError" in lines[0]
    assert "secret" not in lines[0]
    mysql_connection = _PreflightConnection()
    mysql_connection.dialect = SimpleNamespace(name="mysql")
    assert preflight_target(
        "mysql+pymysql://user:secret@db/petrosa_crypto",
        "mysql",
        lines.append,
        _PreflightEngine(mysql_connection),
    )
    assert (
        DEFAULT_MYSQL_TABLE_PREFIX + "benchmark_preflight_probe"
        in mysql_connection.statements[1]
    )
    assert all(
        "benchmark_preflight_probe" not in statement
        or DEFAULT_MYSQL_TABLE_PREFIX in statement
        for statement in mysql_connection.statements
    )


def test_output_matches_final_stdout(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setenv("BENCHMARK_POSTGRES_URL", "postgresql://x")
    monkeypatch.setenv("BENCHMARK_MYSQL_URL", "mysql://x")
    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.run_target",
        lambda url, backend, settings: {"backend": backend},
    )
    output_path = tmp_path / "result.json"
    monkeypatch.setattr("sys.argv", ["benchmark", "--output", str(output_path)])
    main()
    stdout = capsys.readouterr().out.strip().splitlines()[-1]
    assert stdout == output_path.read_text(encoding="utf-8").strip()
    assert '"backend": "postgres"' in stdout


def test_mysql_setup_workload_and_cleanup_only_use_prefixed_tables():
    settings = Settings(
        rows=2, warmups=1, repetitions=1, mysql_table_prefix="bench775_"
    )
    engine = create_engine("sqlite://")
    statements = []
    from sqlalchemy import event

    event.listen(
        engine, "before_cursor_execute", lambda *args: statements.append(args[2])
    )
    _, tables = create_schema(engine, "mysql", settings)
    rows = synthetic_rows(settings)
    measure(engine, tables["klines"], "point_lookup", rows, "mysql", settings)
    cleanup_schema(engine, "mysql", tables)

    table_statements = [
        statement.lower()
        for statement in statements
        if any(
            table in statement.lower()
            for table in ("klines", "signals", "positions", "execution_events")
        )
    ]
    assert table_statements
    assert all("bench775_" in statement for statement in table_statements)
    assert {
        statement.split("`")[1]
        for statement in table_statements
        if "drop table" in statement
    } == {
        "bench775_klines",
        "bench775_signals",
        "bench775_positions",
        "bench775_execution_events",
    }


def test_time_range_lookup_filters_symbol_and_timeframe_and_reports_rows():
    settings = Settings(rows=4, warmups=1, repetitions=1)
    engine = create_engine("sqlite://")
    _, tables = create_schema(engine, "mysql", settings)
    statements = []
    from sqlalchemy import event

    event.listen(
        engine, "before_cursor_execute", lambda *args: statements.append(args[2])
    )
    result = measure(
        engine,
        tables["klines"],
        "time_range_lookup",
        synthetic_rows(settings),
        "mysql",
        settings,
    )
    assert result["status"] == "ok"
    assert result["completed_operations"] == 1
    assert result["rows_returned"] == 1
    assert any(
        "symbol" in statement.lower() and "timeframe" in statement.lower()
        for statement in statements
    )


def test_target_failure_is_recorded_and_later_target_runs(monkeypatch, capsys):
    monkeypatch.setenv("BENCHMARK_POSTGRES_URL", "postgresql://x")
    monkeypatch.setenv("BENCHMARK_MYSQL_URL", "mysql://x")
    calls = []

    def fake_run_target(url, backend, settings):
        calls.append(backend)
        if backend == "postgres":
            raise TimeoutError("timed out")
        return {"backend": backend, "rtt_baseline": {"status": "ok"}, "results": []}

    monkeypatch.setattr(
        "scripts.historic_storage_benchmark.run_target", fake_run_target
    )
    monkeypatch.setattr("sys.argv", ["benchmark"])
    main()
    output = capsys.readouterr().out
    assert calls == ["postgres", "mysql"]
    assert '"status": "timeout"' in output
    assert '"backend": "mysql"' in output
