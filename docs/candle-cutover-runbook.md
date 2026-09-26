# Candle-store cutover runbook

Operational procedure for safely promoting MongoDB to the primary execution
candle store, and for rolling back. Produced by
[`PetroSa2/petrosa-data-manager#275`](https://github.com/PetroSa2/petrosa-data-manager/issues/275);
the flip itself is owned by
[`#274`](https://github.com/PetroSa2/petrosa-data-manager/issues/274). Window
sizing comes from [`docs/candle-consumer-retention-contract.md`](candle-consumer-retention-contract.md)
(#276): **`MIN_WARMUP_CANDLES = 400`** per `(pair, timeframe)`.

When following linked GitHub issues from an MCP-capable agent runtime, use the
official `github` MCP server. Use `gh` only as the fallback for non-MCP clients,
deterministic scripts, runners, or unsupported operations, with authentication from
the configured file-backed token or environment.

## Why this is not a config flip

Candle **writes** land in MongoDB `klines_*`, the operational candle store.
MySQL `klines_*` is a historic reference copy only. The readiness and warm-up
steps below protect the operational Mongo collections from being empty or stale.

### Two corrections to the ticket's premise, found during implementation

1. **The read path is not hardwired to Mongo.** `CandleRepository.get_range`
   has always branched on `CANDLE_DATABASE_TYPE`
   (`data_manager/db/repositories/candle_repository.py`), so
   `/data/candles` reads whichever backend is configured. What *was* hardwired
   is the response **metadata**: `source: "mongodb"` and
   `collection: candles_{pair}_{period}` were emitted as string literals no
   matter which store answered. Both now report the backend that actually
   served the read.
2. **The endpoint is `/data/candles`, not `/api/v1/candles`.** The `data`
   router is mounted at `/data` (`data_manager/api/app.py`), and
   `petrosa-bot-ta-analysis` calls `GET /data/candles`
   (`ta_bot/services/dm_sdk/client.py`). Use that path when verifying.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CANDLE_WARMUP_MIN_CANDLES` | `400` | Depth every `candles_*` collection must reach before the flip (#276 contract). |
| `CANDLE_WARMUP_FRESHNESS_INTERVALS` | `3` | Newest candle may be at most this many intervals old to count as fresh. |
| `CANDLE_WARMUP_BATCH_SIZE` | `500` | Documents per Mongo `insert_many` during warm-up. |
| `CANDLE_WARMUP_TRIM_ENABLED` | `true` | Self-bounding capped-count trim on collections the warm-up job touches. |
| `CANDLE_WARMUP_TRIM_FACTOR` | `1.5` | Keep `MIN_WARMUP_CANDLES x factor` documents per collection. |
| `CANDLE_READ_FALLBACK_ENABLED` | `true` | AC3 safety net: serve reads from the non-primary backend when the primary returns an empty/short window. **Kill-switch — set `false` to restore single-backend reads.** |
| `CANDLE_DUAL_WRITE_ENABLED` | `false` | AC4: mirror candle writes into the non-primary backend so a rollback cannot find a hole. Enable for the cutover window only. |
| `SUPPORTED_INTERVALS` | `5m,15m,30m,1h,1d` | Timeframes the warm-up/readiness grid covers (shared configmap key). |
| `CANDLE_DATABASE_TYPE` | `mongodb` | MongoDB operational store; MySQL is historic-only. |

> **Standing rule:** never leave anything writing to MongoDB without a bound
> and an off-flag. Atlas M0 (512 MB) has produced four quota P0s
> (k8s#783/#819/#881/#899). The warm-up job bounds itself via the trim above;
> ongoing `candles_*` retention is #274 AC3, implemented in
> [`data_manager/maintenance/candles_retention.py`](../data_manager/maintenance/candles_retention.py)
> — see [`docs/candles-retention.md`](candles-retention.md).

## Cutover procedure

### 1. Warm Mongo (AC1)

```bash
# Dry run first — reports what would be written/trimmed, touches nothing.
python -m data_manager.maintenance.candle_warmup_backfill --dry-run

# Real run. Idempotent and resumable: re-running is safe and cheap, and
# collections that already pass the readiness gate are skipped.
python -m data_manager.maintenance.candle_warmup_backfill
```

Scope it down while testing with `--pairs BTCUSDT --timeframes 1h`, or force a
re-copy of already-warm collections with `--force`.

The job reads the newest `CANDLE_WARMUP_MIN_CANDLES` rows per
`(pair, timeframe)` from the historic MySQL `klines_*` tables and writes them into
`candles_{pair}_{timeframe}`. Deduplication is structural:
`MongoDBAdapter.write` derives `_id` from `symbol`+`timestamp` and inserts with
`ordered=False`, so duplicates are no-ops rather than errors.

### 2. Verify readiness (AC2 — fail-closed gate)

```bash
# Exit 0 = every pair/timeframe ready; exit 1 = flip must NOT proceed.
python -m data_manager.maintenance.candle_readiness --json
```

or, against a running pod:

```bash
curl -s "$DM/data/candles/readiness" | jq '.ready, (.collections[] | select(.ready==false))'
```

A collection is ready only when it is **deep** (>= `MIN_WARMUP_CANDLES`) *and*
**fresh** (newest candle within `CANDLE_WARMUP_FRESHNESS_INTERVALS` x its own
timeframe). Everything else — missing collection, unreachable Atlas,
unparseable timeframe, an empty grid — resolves to **not ready**. The gate
never fails open.

**Do not proceed to step 3 while `ready` is `false`.**

### 3. Enable dual-write, then flip (AC4 prerequisite + #274)

```bash
# 1. Mirror writes so MySQL keeps receiving candles after the flip.
kubectl set env deploy/petrosa-data-manager CANDLE_DUAL_WRITE_ENABLED=true
# 2. Re-check readiness (step 2) once the rollout settles.
# 3. Flip the primary store (#274).
kubectl set env deploy/petrosa-data-manager CANDLE_DATABASE_TYPE=mongodb
```

Order matters. Dual-write must be live **before** the flip, otherwise MySQL
develops a gap for the period Mongo was primary and the rollback in the next
section would read an incomplete window.

`CANDLE_READ_FALLBACK_ENABLED` stays `true` throughout: if any collection turns
out to be short despite the gate, the read is transparently served from MySQL
instead of returning a starved window (AC3).

### 4. Verify post-flip (AC5)

```bash
# Backend that actually answered + completeness of the requested window.
curl -s "$DM/data/candles?pair=BTCUSDT&period=1h&limit=250" \
  | jq '.metadata.source, .metadata.data_completeness, (.data | length)'
```

Expect `"mongodb"`, `data_completeness` at/near `100`, and `250` rows. Then the
synthetic strategy-lookback check, at the true contract depth:

```bash
for tf in 5m 15m 30m 1h 1d; do
  curl -s "$DM/data/candles?pair=BTCUSDT&period=$tf&limit=400" \
    | jq -r "\"$tf \\(.metadata.source) \\(.data | length) \\(.metadata.data_completeness)\""
done
```

Finally, confirm the safety net is idle — a non-zero rate here means the warm
path is incomplete and the cutover should be rolled back:

```promql
rate(data_manager_candle_read_fallbacks_total[5m])
```

`data_manager_candle_dual_writes_total{outcome="error"}` should likewise stay
flat; a rising error rate means a rollback would land on incomplete MySQL data.

> **Note on the lookback window.** `/data/candles` previously defaulted to a
> hardcoded 24-hour window when `start` was omitted, so `limit=250&period=1h`
> could return at most 24 rows and `period=1d` at most 1 — a guaranteed partial
> window, which AC3 forbids. The default window is now derived from
> `offset + limit` candles' worth of the requested `period`. Callers that pass
> an explicit `start` are unaffected.

## Rollback (AC4)

Reversible at any point, in this order:

```bash
# 1. Obtain operator approval and merge a reviewed revert PR before changing the store.
#    Do not use kubectl set env for a production rollback.
# 2. Verify reads are served by the historic copy and complete.
curl -s "$DM/data/candles?pair=BTCUSDT&period=1h&limit=250" | jq '.metadata.source'
# 3. Only once verified, stop mirroring.
kubectl set env deploy/petrosa-data-manager CANDLE_DUAL_WRITE_ENABLED=false
```

No data is lost: the optional historic copy can support an approved rollback,
and the Mongo collections are left in place (bounded by the warm-up
trim / #274 retention) so a second attempt does not have to re-warm from
scratch. There is no empty-read window on the way back either — the fallback is
symmetric, so a MySQL miss during the rollback is served from Mongo.

If reads misbehave in a way the fallback is masking, turn the safety net off to
see the raw primary behaviour:

```bash
kubectl set env deploy/petrosa-data-manager CANDLE_READ_FALLBACK_ENABLED=false
```

## Scheduling the warm-up job

The warm-up is a `petrosa_k8s` manifest change (Job or CronJob), tracked
separately from this repo's PR — it follows the pattern of the existing
`klines_retention` CronJob:

```yaml
command: ["python", "-m", "data_manager.maintenance.candle_warmup_backfill"]
```

Run it repeatedly (it is idempotent and skips warm collections) until the
readiness gate returns exit 0, then keep it on a schedule until #274 lands so
the collections cannot go cold before the flip.

## Status of #274 (this ticket)

- **AC1/AC2 (Mongo primary for execution, MySQL demoted):** the code-level
  write/read mismatch was already resolved here in #275/#276 — the read
  path (`CandleRepository.get_range`) has always branched on
  `CANDLE_DATABASE_TYPE`; only the response metadata was previously
  hardwired, and that is now fixed too. Flipping `CANDLE_DATABASE_TYPE` in
  production is the operational step in "Cutover procedure" step 3 above —
  it is a `petrosa_k8s` ConfigMap change (`k8s/data-manager/configmap.yaml`)
  gated on the AC2 readiness check passing live, not a data-manager code
  change. **Not performed by the #274 PR** in this repo: it requires
  confirming the live cluster's `candles_*` collections have actually been
  warmed via `candle_warmup_backfill` first (a runtime state this repo's
  code cannot self-certify), so the flip is left as the next
  operator-executed step in this runbook rather than bundled into an
  automated code PR.
- **AC3 (mandatory TTL/retention on `candles_*`):** implemented —
  [`docs/candles-retention.md`](candles-retention.md).
- **AC4 (kill-switch):** already implemented by #275 —
  `CANDLE_DATABASE_TYPE=mysql` reverts the primary store, and
  `CANDLE_READ_FALLBACK_ENABLED`/`CANDLE_DUAL_WRITE_ENABLED` provide the
  transition safety net. No code change needed for #274.
- **AC5 (retention observability):** implemented as part of AC3 above.
- **AC7 (verification):** "Verify post-flip (AC5)" section above covers this;
  the checks are backend-agnostic and apply equally to this ticket's flip.

## Related

- [`#274`](https://github.com/PetroSa2/petrosa-data-manager/issues/274) — primary-store flip, retention/TTL, kill-switch.
- [`#276`](https://github.com/PetroSa2/petrosa-data-manager/issues/276) / [`docs/candle-consumer-retention-contract.md`](candle-consumer-retention-contract.md) — where `MIN_WARMUP_CANDLES = 400` comes from.
- [`docs/klines-retention.md`](klines-retention.md) — sibling retention job for the MySQL-tier `klines_*` collections.
- [`PetroSa2/petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783) — Atlas M0 quota P0 epic.
- [`PetroSa2/petrosa_k8s#794`](https://github.com/PetroSa2/petrosa_k8s/issues/794) — candle-backend audit that surfaced the write/read mismatch.
