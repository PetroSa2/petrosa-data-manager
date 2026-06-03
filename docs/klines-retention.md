# Klines retention

Periodic deletion of old klines from MongoDB to prevent the shared Atlas cluster
from re-filling its storage quota. Implements **AC2** of the umbrella incident
[`PetroSa2/petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783)
and is tracked under [`PetroSa2/petrosa-data-manager#210`](https://github.com/PetroSa2/petrosa-data-manager/issues/210).

## What it does

Walks every `klines_*` collection in MongoDB, computes a per-timeframe cutoff
(`now - retention_window`), and deletes all klines older than the cutoff in
day-sized chunks. The chunked walk keeps a single run's blast radius bounded
so a long-accumulated backlog is reclaimed across several scheduled invocations
rather than one multi-million-document `delete_many`.

The job is **idempotent**: re-running it on a freshly-pruned collection is a
no-op (no eligible docs → zero deletes).

## How to run

The maintenance module is invokable directly:

```bash
# Dry-run: count what would be deleted, modify nothing.
python -m data_manager.maintenance.klines_retention --dry-run

# Live run with defaults from env.
python -m data_manager.maintenance.klines_retention

# Override the collection set or pacing.
python -m data_manager.maintenance.klines_retention \
    --collections klines_1m,klines_5m \
    --batch-days 1 \
    --max-chunks 50
```

In production it is driven by a Kubernetes CronJob defined in
[`PetroSa2/petrosa_k8s`](https://github.com/PetroSa2/petrosa_k8s) (companion ticket
tracks the manifest — see #210's PR description for the link). The container
runs the same `python -m data_manager.maintenance.klines_retention` command
under `opentelemetry-instrument` so logs and traces hit the standard exporter.

## Configuration

All knobs are environment-driven so Helm/CronJob env can override per cluster.

### Retention windows

Per-timeframe windows (in days) override the in-code defaults:

| Env var | Default (days) |
|---|---|
| `KLINES_RETENTION_DAYS_1M` | 7 |
| `KLINES_RETENTION_DAYS_3M` | 14 |
| `KLINES_RETENTION_DAYS_5M` | 14 |
| `KLINES_RETENTION_DAYS_15M` | 30 |
| `KLINES_RETENTION_DAYS_30M` | 30 |
| `KLINES_RETENTION_DAYS_1H` | 90 |
| `KLINES_RETENTION_DAYS_2H` | 90 |
| `KLINES_RETENTION_DAYS_4H` | 180 |
| `KLINES_RETENTION_DAYS_6H` | 180 |
| `KLINES_RETENTION_DAYS_8H` | 180 |
| `KLINES_RETENTION_DAYS_12H` | 365 |
| `KLINES_RETENTION_DAYS_1D` | 365 |
| `KLINES_RETENTION_DAYS_3D` | 365 |
| `KLINES_RETENTION_DAYS_1W` | 730 |

Each default exceeds the longest strategy lookback at that timeframe by a
comfortable margin so analysis is never starved by deletion. Tune downward
only when you understand the lookbacks of every active strategy.

The Binance `1M` (uppercase, monthly) timeframe is intentionally **not** in
the override map — its env-var name would collide with `1m` (minute) after
upper-casing. A `klines_1M` collection, if one is ever persisted, falls
through to the `365`-day fallback. File a follow-up if/when monthly klines
need an independent override.

An unknown timeframe (collection lands before this map is updated) falls back
to `365` days. The fallback is conservative on purpose.

### Pacing knobs

| Env var | Default | Meaning |
|---|---|---|
| `KLINES_RETENTION_BATCH_DAYS` | `1` | Width of each chunked `delete_range` window. Smaller = more, smaller deletes per run. |
| `KLINES_RETENTION_MAX_CHUNKS_PER_COLLECTION` | `400` | Per-run cap on chunks per collection. Once hit, the run logs `capped=true` and stops; the next scheduled run resumes where it left off. |
| `KLINES_RETENTION_DRY_RUN` | unset | Set to `true` to force dry-run mode regardless of `--dry-run`. |

### Connection

Reuses the canonical `MONGODB_URL` env var (same one used by the rest of the
data-manager service). The job exits with code `2` when it is not set.

## Observability

Every chunk emits a structured INFO log of the form:

```
klines_retention: klines_1h chunk 3 2026-05-31T00:00:00+00:00 → 2026-06-01T00:00:00+00:00 — 1440 docs
```

On completion the job summarises the run:

```
klines_retention: run complete — 14 collections processed, 482917 docs deleted
```

Run under `opentelemetry-instrument` (the production pattern, mirroring the
existing kline CronJobs in petrosa_k8s) these logs/spans flow into the standard
Petrosa OTel pipeline. The `>80%` Atlas storage alert that closes the loop is
tracked under [`PetroSa2/petrosa_k8s#786`](https://github.com/PetroSa2/petrosa_k8s/issues/786).

## Safety properties

- **Bounded**: per-run chunks-per-collection cap; per-chunk window is day-sized
  by default so a single delete cannot grow unbounded.
- **Idempotent**: re-running on a freshly-pruned collection deletes zero docs.
- **Symbol-agnostic**: deletes purely by timestamp, never by symbol — so a new
  symbol can light up at any time and inherit retention automatically.
- **Non-cross-collection**: only touches `klines_*` collections. Audit-trail
  collections (`intents`, `cio_decisions`, `execution_events`, `pnl_events`)
  and the configuration/alert collections are not in scope; their retention is
  governed independently.

## Related

- [`PetroSa2/petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783) — P0 incident this was filed to prevent recurring.
- [`PetroSa2/petrosa_k8s#786`](https://github.com/PetroSa2/petrosa_k8s/issues/786) — `>=80%` Atlas storage Grafana alert (AC3 of #783).
- [`PetroSa2/petrosa_k8s#739`](https://github.com/PetroSa2/petrosa_k8s/issues/739) — audit-trail Mongo backup + retention CronJob (the in-house CronJob precedent this retention job mirrors).
