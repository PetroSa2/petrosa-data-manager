# Candles retention (`candles_*`)

Periodic capped-count deletion of `candles_{pair}_{timeframe}` documents from
MongoDB. Implements **AC3** (mandatory, blocking) of
[`PetroSa2/petrosa-data-manager#274`](https://github.com/PetroSa2/petrosa-data-manager/issues/274)
— before this job, `candles_*` collections were never discovered or pruned by
`klines_retention.py` (its `KLINES_COLLECTION_PREFIX` filter only matches
`klines_`), leaving the Mongo hot-path namespace unbounded. That gap is
exactly the shape of the four Atlas M0 quota P0 outages
([`petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783)/[`#819`](https://github.com/PetroSa2/petrosa_k8s/issues/819)/[`#881`](https://github.com/PetroSa2/petrosa_k8s/issues/881)/[`#899`](https://github.com/PetroSa2/petrosa_k8s/issues/899)).

## Why capped-count, not calendar-day

`klines_retention.py` prunes by a per-timeframe calendar-day cutoff, which
assumes a roughly steady insertion rate. `docs/candle-consumer-retention-contract.md`
AC4 recommends a **capped-count** job for `candles_*` instead: every
execution-path consumer (`petrosa-bot-ta-analysis`) needs the same candle
*count* regardless of timeframe (`MIN_WARMUP_CANDLES = 400`, sized at 1.5x the
true max strategy lookback of 265). A fixed-count cap stays bounded
regardless of gaps in candle cadence — a day-based TTL under-retains a 1d
collection (400 candles ≈ 13 months) if sized off a `5m` day-budget, and
over-retains a `5m` collection if sized off `1d`.

## What it does

Walks every `candles_*` collection, and for any collection holding more than
`CANDLES_RETENTION_MAX_COUNT` documents (default 400, see below), deletes
everything older than the boundary between the newest N docs (kept) and the
rest (pruned) in a single bounded `delete_range` call per collection.

Idempotent: re-running on a collection already at or under the cap is a
no-op.

## How to run

```bash
# Dry-run: count what would be deleted, modify nothing.
python -m data_manager.maintenance.candles_retention --dry-run

# Live run with defaults from env.
python -m data_manager.maintenance.candles_retention

# Override the collection set or cap.
python -m data_manager.maintenance.candles_retention \
    --collections candles_BTCUSDT_1h,candles_ETHUSDT_1h \
    --max-count 400
```

Scheduling this as a Kubernetes CronJob is a `petrosa_k8s` manifest change,
tracked separately from this repo's PR — it follows the same pattern as the
existing `klines-retention` CronJob (`docs/klines-retention.md`):

```yaml
command: ["python", "-m", "data_manager.maintenance.candles_retention"]
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `CANDLES_RETENTION_MAX_COUNT` | `CANDLE_WARMUP_MIN_CANDLES` (400) | Max documents kept per `candles_*` collection. Mirrors the #275/#276 warm-up depth so a freshly cut-over collection is never trimmed below the depth it was just backfilled to. |
| `MONGODB_URL` | — | Same connection string used by the rest of the service. Job exits `2` if unset. |

An unknown/malformed override falls back to the default and logs a warning —
the job never silently disables itself on a bad env value.

## Observability (AC5)

Each collection emits a structured INFO log with docs deleted and an
estimated bytes-reclaimed figure (`docs_deleted x ~330 bytes/candle`, the
per-document footprint derived in
`docs/candle-consumer-retention-contract.md` AC3):

```
candles_retention: candles_BTCUSDT_1h — 500 docs before, cap 400, 100 docs deleted (~33000 bytes est.), cutoff=2026-06-01T00:00:00+00:00
```

On completion:

```
candles_retention: run complete — 50 collections processed, 1200/21200 docs deleted (~396000 bytes est. reclaimed)
```

The Atlas `>80%` data-size alert
([`petrosa_k8s#786`](https://github.com/PetroSa2/petrosa_k8s/issues/786) /
[`#905`](https://github.com/PetroSa2/petrosa_k8s/issues/905)/[`#920`](https://github.com/PetroSa2/petrosa_k8s/issues/920))
covers the `candles_*` namespace's contribution to overall cluster usage;
this job's logs are the per-collection detail behind that aggregate.

## Safety properties

- **Bounded**: one `delete_range` per collection per run, anchored at a
  count-derived cutoff — never an unbounded `delete_many`.
- **Idempotent**: re-running on a collection already at/under the cap deletes
  zero docs.
- **Fail-soft per collection**: a backend error on one collection (count,
  `query_latest`, or delete) is logged and that collection is skipped with
  zero deletions; it never aborts the whole run.
- **Consistent with the warm-up floor**: default cap ==
  `CANDLE_WARMUP_MIN_CANDLES`, so retention and warm-up/readiness
  (`data-manager#275`) never disagree about "how much history is enough."

## Related

- [`PetroSa2/petrosa-data-manager#274`](https://github.com/PetroSa2/petrosa-data-manager/issues/274) — primary-store flip, AC3 (this job), kill-switch.
- [`docs/candle-consumer-retention-contract.md`](candle-consumer-retention-contract.md) — where `MIN_WARMUP_CANDLES = 400` and the capped-count recommendation (AC4) come from.
- [`docs/candle-cutover-runbook.md`](candle-cutover-runbook.md) — the cutover procedure this retention job de-risks.
- [`docs/klines-retention.md`](klines-retention.md) — sibling job for the MySQL-tier `klines_*` collections (calendar-day based).
- [`PetroSa2/petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783) — Atlas M0 quota P0 incident epic.
