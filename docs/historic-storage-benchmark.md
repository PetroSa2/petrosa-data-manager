# Historic Storage Benchmark

This harness compares PostgreSQL and MySQL using synthetic rows only.
It creates or uses only the isolated PostgreSQL schema `benchmark_mysql_vs_postgres`.
MySQL uses the database already named in `BENCHMARK_MYSQL_URL`, with a separate,
prefixed table namespace so it never targets production table names.

## Run

The image packages the PostgreSQL driver used by this harness. Do not install drivers
at runtime. Run preflight first when testing target credentials or connectivity:

```bash
python scripts/historic_storage_benchmark.py --preflight
```

Provide URLs through the environment, not source files, issue text, or committed output.
The PostgreSQL URL must target `psql_total` and include `sslmode=require`.
The MySQL URL may target the shared data-manager database. The table prefix is
validated as a non-empty SQL identifier prefix and defaults to `bench775_`.

```bash
export BENCHMARK_POSTGRES_URL='postgresql://USER:PASSWORD@psql_total.postgresql.dbaas.com.br:5432/psql_total?sslmode=require'
export BENCHMARK_MYSQL_URL='mysql+pymysql://USER:PASSWORD@MYSQL_HOST:3306/MYSQL_DATABASE'
python scripts/historic_storage_benchmark.py --dry-run
python scripts/historic_storage_benchmark.py --rows 1000 --mysql-table-prefix bench775_ \
  --output /secure/local/benchmark-result.json
```

The result contains no credentials and records timestamps, row counts, schema,
workload parameters, p50/p95/p99 latency, throughput, errors, and pool size.
The default run uses one warm-up, three measured repetitions, four concurrent workers,
250-row batches, ten symbols, and 1,000 synthetic rows per table.
Each target is preflighted automatically with a ten-second connection timeout before
loading rows. Progress is printed per backend and workload, including row counts and
elapsed seconds; bulk ingest emits additional progress at least every 30 seconds.
The final JSON is printed to stdout and written unchanged to `--output` when supplied.
The MySQL engine is capped at four total connections (`pool_size=4` and
`max_overflow=0`), including concurrent workloads.
Before setup, the harness drops only the four known prefixed benchmark tables with
explicit `DROP TABLE IF EXISTS` statements; it never creates a database or uses
wildcard cleanup.

## Workloads and capability review

The harness measures bulk ingest, batched upsert, point lookup, time-range lookup,
latest-per-symbol lookup, aggregate/statistical query, concurrent reads, concurrent
writes, and bounded backfill/replay.

The run output must be attached to the issue only after checking it contains no URLs
with credentials. The issue recommendation must state whether evidence supports keeping
MySQL, repeating a controlled experiment, or planning migration.

| Capability | Status | Evidence or verification method |
|---|---|---|
| SQL/schema compatibility | compatible | Equivalent SQLAlchemy table definitions and successful setup |
| Composite indexes | compatible | `(symbol, event_time)` index is created on every table |
| Covering indexes | untested | Add and measure backend-specific covering indexes in a follow-up isolated run |
| Partitioning | untested | Provider DDL review; not exercised by this harness |
| Upsert semantics | compatible | Dialect-native conflict handling and replay workload |
| JSON/temporal types | compatible | JSON payload and timezone-aware event columns |
| Transactions/isolation | untested | Provider defaults are recorded by the run owner; compare in follow-up |
| Pooling/observability | untested | Pool size and error counts are recorded; provider metrics are external |
| Backup/restore/provider limits | untested | Provider documentation and billing/quota review |
| Migration effort | requires redesign | Separate design review; no migration is performed |
| Data-manager adapter effort | requires redesign | Separate implementation ticket; no production adapter is changed |

The shared-account limits, provider observations, and measurement noise must be called
out in the issue evidence. Kubernetes Secret creation, provider permissions, billing,
quota, backup, retention, and cleanup remain operator-owned read-only prerequisites.
