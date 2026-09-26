# Continuous candle warm-up backfill

**Ticket:** [petrosa-data-manager#319](https://github.com/PetroSa2/petrosa-data-manager/issues/319) ·
**Parent:** [#317](https://github.com/PetroSa2/petrosa-data-manager/issues/317) (unified candle gap filling pipeline) ·
**Predecessors:** [#275](https://github.com/PetroSa2/petrosa-data-manager/issues/275) (warm-up backfill), [#276](https://github.com/PetroSa2/petrosa-data-manager/issues/276) (retention contract), [#274](https://github.com/PetroSa2/petrosa-data-manager/issues/274) (Mongo primary flip)

## The problem this solves

`data_manager/maintenance/candle_warmup_backfill.py` was built for exactly one
moment: the #274 cutover. MongoDB is the operational execution candle store,
and the continuous loop keeps it warm after the readiness gate passes.

Anything that removed candles *after* that moment stayed removed:

- a missed extractor window,
- an Atlas hiccup during a write burst,
- a new pair added to `SUPPORTED_PAIRS` (its collection starts at zero),
- the retention job trimming a collection that never got refilled.

Collections silently drop below the 400-candle depth contract from #276, and
strategy evaluation quietly reads a short window. Nothing alerts, because
nothing was measuring.

## Design decision — in-process loop, not a CronJob

AC1 asked for the continuous-vs-scheduled decision. Both designs were
evaluated; the **in-process periodic loop won**, and the CronJob path remains
available as a manual escape hatch.

| | In-process loop (chosen) | Kubernetes CronJob |
|---|---|---|
| Metrics delivery | Deployment is an existing live scrape target — counters are visible immediately | Short-lived pods push OTLP and routinely die before the final export flushes, so an alert-critical counter can be lost |
| Single writer | `LeaderElectionManager` already guarantees one active writer across replicas | Needs its own `concurrencyPolicy`; an overlapping slow run double-writes |
| Credentials / adapters | Already connected in-process (`db_manager.mysql_adapter`, `.mongodb_adapter`) | New manifest, new secret wiring, new image-tag lifecycle — in a *different* repo (`petrosa_k8s`) |
| Cadence changes | ConfigMap env var, no manifest churn | Manifest edit + GitOps round-trip |
| Blast radius | One asyncio task; failure isolated, process unaffected | Failed pods accumulate, need `failedJobsHistoryLimit` tuning |

The deciding factor is the first row: AC4 and AC5 only mean something if the
counter reliably reaches Grafana. The CronJob metric path in this ecosystem is
OTLP-push from a pod that exits seconds later — a known source of dropped final
exports. The Deployment path is scraped.

**The CronJob route is not deleted.** The CLI entrypoint from #275 is untouched,
so an operator can still force an out-of-band sweep (see *Manual sweep* below),
and a CronJob manifest can be added to `petrosa_k8s/k8s/data-manager/` later
without touching this code.

## How it works

`data_manager/maintenance/candle_warmup_scheduler.py` — `CandleWarmupScheduler`.

1. Started as a background task by `DataManagerApp._run_candle_warmup_scheduler()`.
2. Refuses to run unless `ENABLE_CANDLE_WARMUP_SCHEDULER=true`.
3. Refuses to run unless this pod is the leader (fail-closed: leader election
   enabled but no manager present ⇒ no writes).
4. Sleeps `CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY` so the pod passes its
   readiness probe first.
5. Every `CANDLE_WARMUP_SCHEDULER_INTERVAL` seconds, calls the **existing**
   `run_backfill()` from #275 over the full `SUPPORTED_PAIRS × SUPPORTED_INTERVALS`
   grid. No backfill logic is duplicated.
6. Each collection that still passes the #275 readiness gate (depth ≥
   `CANDLE_WARMUP_MIN_CANDLES` **and** newest candle younger than
   `CANDLE_WARMUP_FRESHNESS_INTERVALS × timeframe`) is **skipped** — AC3. A
   healthy grid therefore costs 50 cheap count/latest reads and zero writes.
7. Records metrics, then sleeps again. A cycle that raises is counted, logged
   and retried after `CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF` — the loop never dies.

### Write safety

Every cycle delegates to the #275 backfill, which trims each touched collection
back to `CANDLE_WARMUP_MIN_CANDLES × CANDLE_WARMUP_TRIM_FACTOR` documents. The
job cannot grow the namespace no matter how often it runs — the standing rule
after four Atlas M0 quota P0s (`petrosa_k8s#783/#819/#881/#899`): nothing writes
to MongoDB without a bound **and** an off-flag. Both are present.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ENABLE_CANDLE_WARMUP_SCHEDULER` | `false` | Master off-flag. **Opt-in** — a new Mongo writer never enables itself. |
| `CANDLE_WARMUP_SCHEDULER_INTERVAL` | `3600` | Seconds between cycles. Floor of 60s enforced in code. |
| `CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY` | `300` | Delay before the first cycle after pod start. |
| `CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF` | `300` | Sleep after a cycle raises. |

Inherited from #275 and unchanged: `CANDLE_WARMUP_MIN_CANDLES` (400),
`CANDLE_WARMUP_FRESHNESS_INTERVALS` (3), `CANDLE_WARMUP_BATCH_SIZE` (500),
`CANDLE_WARMUP_TRIM_ENABLED` (true), `CANDLE_WARMUP_TRIM_FACTOR` (1.5).

### Why hourly

The tightest freshness budget in the grid is 5m × 3 = 15 minutes, but the
scheduler is a **depth** maintainer, not a real-time gap filler — near-real-time
gaps are the job of the streaming gap detector (#322/#323) and the extractor's
gap filler ([binance-data-extractor#300](https://github.com/PetroSa2/petrosa-binance-data-extractor/issues/300)).
Hourly restores depth well inside any strategy's tolerance while keeping the
read cost trivial. Lower it if the metrics below show a persistent backlog.

## Metrics (AC4)

| Metric | Type | Labels | Use |
|---|---|---|---|
| `data_manager_candle_backfill_collections_total` | counter | `timeframe`, `outcome` | **AC4 metric.** `outcome` ∈ `written` \| `skipped` \| `failed` \| `no_source_data` |
| `data_manager_candle_backfill_candles_written_total` | counter | `timeframe` | Volume actually restored |
| `data_manager_candle_backfill_cycles_total` | counter | `outcome` (`success`\|`failed`) | Cycle-level health |
| `data_manager_candle_backfill_cycle_seconds` | histogram | — | Sweep duration |
| `data_manager_candle_backfill_last_success_timestamp` | gauge | — | Epoch seconds of last clean sweep — the *falling behind* signal |
| `data_manager_candle_backfill_collections_needing_warmup` | gauge | — | Collections that failed the readiness gate last cycle. Steady state **0** |
| `data_manager_candle_backfill_active` | gauge | — | 1 on the leader pod running the loop |

`outcome="no_source_data"` is deliberately **not** folded into `skipped`. A dry
source table is unhealthy; conflating it with "already warm" would hide exactly
the staleness this ticket exists to catch.

## Alerts (AC5)

Add to the data-manager alert rules in `petrosa_k8s`. Both conditions the AC
names — *fails* and *falls behind* — plus a backlog rule.

```yaml
groups:
  - name: data-manager-candle-warmup
    rules:
      # 1. Backfill is failing.
      - alert: CandleWarmupBackfillFailing
        expr: increase(data_manager_candle_backfill_cycles_total{outcome="failed"}[2h]) >= 2
        for: 10m
        labels:
          severity: warning
          service: data-manager
        annotations:
          summary: "Candle warm-up backfill failing (>=2 failed cycles in 2h)"
          runbook: "docs/candle-warmup-continuous-backfill.md#triage"

      # 2. Backfill has fallen behind — no clean sweep in 3 intervals.
      #    The `> 0` guard keeps the alert quiet before the first sweep.
      - alert: CandleWarmupBackfillStale
        expr: >
          data_manager_candle_backfill_last_success_timestamp > 0
          and (time() - data_manager_candle_backfill_last_success_timestamp) > 10800
        for: 15m
        labels:
          severity: warning
          service: data-manager
        annotations:
          summary: "No successful candle warm-up sweep in over 3 hours"
          runbook: "docs/candle-warmup-continuous-backfill.md#triage"

      # 3. The sweep runs but cannot keep up — collections stay cold.
      - alert: CandleWarmupBacklogPersistent
        expr: data_manager_candle_backfill_collections_needing_warmup > 5
        for: 2h
        labels:
          severity: warning
          service: data-manager
        annotations:
          summary: "{{ $value }} candle collections still below depth after repeated sweeps"
          runbook: "docs/candle-warmup-continuous-backfill.md#triage"

      # 4. The loop is enabled but nothing is running it (no leader).
      - alert: CandleWarmupSchedulerInactive
        expr: max(data_manager_candle_backfill_active) == 0
        for: 30m
        labels:
          severity: info
          service: data-manager
        annotations:
          summary: "Candle warm-up scheduler enabled but no pod is active"
          runbook: "docs/candle-warmup-continuous-backfill.md#triage"
```

## Operator runbook (AC6)

### Enable it

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml --insecure-skip-tls-verify=true \
  -n petrosa-apps set env deployment/petrosa-data-manager \
  ENABLE_CANDLE_WARMUP_SCHEDULER=true
```

Prefer the durable route: set it in
`petrosa_k8s/k8s/data-manager/configmap.yaml` and let GitOps roll it.

Confirm within `CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY + interval`:

```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml --insecure-skip-tls-verify=true \
  -n petrosa-apps logs deployment/petrosa-data-manager | grep candle_warmup_scheduler
```

Expect `candle_warmup_scheduler: starting (interval=3600s, ...)` then
`cycle complete — 50 collections, 0 warmed, 50 already warm, ...`.

### Disable it (kill-switch)

```bash
kubectl ... set env deployment/petrosa-data-manager ENABLE_CANDLE_WARMUP_SCHEDULER=false
```

The loop exits at the next check; no in-flight write is left partial (each
Mongo write is an idempotent `insert_many(ordered=False)` keyed on
symbol+timestamp).

### Triage

**`CandleWarmupBackfillFailing`**
Check which collections failed:

```bash
kubectl ... logs deployment/petrosa-data-manager | grep "cycle finished with"
```

Near-always one of: MySQL `klines_*` unreachable (source), Atlas write
rejection (quota — check `data_manager_mongo_data_size_bytes`), or a schema
drift in a `klines_*` table. Per-collection failures never abort the sweep, so
the other 49 collections are still being maintained.

**`CandleWarmupBackfillStale`**
Either the leader pod is gone (check `data_manager_candle_backfill_active`; if
0, leader election lost quorum) or every recent cycle had a failure. Check the
failing alert first — they usually fire together.

**`CandleWarmupBacklogPersistent`**
The sweep is running but the grid stays cold. That means candles are being
*lost* faster than they are restored, which is an upstream problem, not a
backfill problem:

1. Is the extractor writing? `binance_extractor_candles_written_mongodb_total`.
2. Is retention over-trimming? Compare `CANDLES_RETENTION_MAX_COUNT` against
   `CANDLE_WARMUP_MIN_CANDLES` — retention must be **≥** warm-up depth or the
   two jobs fight each other (#276 contract).
3. Is the source table itself short? `outcome="no_source_data"` on the AC4
   counter means MySQL has nothing to copy — fix the extractor, not this job.

**`CandleWarmupSchedulerInactive`**
Enabled but no active pod. Check `ENABLE_LEADER_ELECTION` and the lease:

```bash
kubectl ... -n petrosa-apps get lease | grep data-manager
```

### Manual sweep (CronJob-style, out of band)

The #275 CLI is unchanged and safe to run at any time — it is idempotent and
self-bounding:

```bash
# Dry run first: reports what would be written/trimmed, touches nothing.
python -m data_manager.maintenance.candle_warmup_backfill --dry-run

# Warm only what is cold (readiness gate skips healthy collections).
python -m data_manager.maintenance.candle_warmup_backfill

# Force a full re-warm of two collections, ignoring the readiness gate.
python -m data_manager.maintenance.candle_warmup_backfill \
  --pairs BTCUSDT --timeframes 5m,1h --force
```

Check readiness independently (exit code 0 = whole grid warm):

```bash
python -m data_manager.maintenance.candle_readiness --json
```

## Related

- [`docs/candle-cutover-runbook.md`](candle-cutover-runbook.md) — the one-shot #275 procedure this automates
- [`docs/candle-consumer-retention-contract.md`](candle-consumer-retention-contract.md) — where the 400-candle depth comes from
- [`docs/candles-retention.md`](candles-retention.md) — the trimming counterpart; must stay ≥ warm-up depth
- [`docs/gap-detection-filling-pipeline.md`](gap-detection-filling-pipeline.md) — the near-real-time half of #317
