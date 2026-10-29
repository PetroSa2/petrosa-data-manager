import pytest

from scripts.historic_storage_benchmark import (
    SCHEMA,
    WORKLOADS,
    Settings,
    redact_url,
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
