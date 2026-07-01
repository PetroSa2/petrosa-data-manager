# Trades Retention (data-manager#246)

Root-cause fix for the **4th** MongoDB Atlas M0 quota P0 (2026-07-01) — the first
one driven by the `trades` collection rather than `intents` (#244).

## What was broken

The `trades` collection holds raw public-trade ticks written **directly by the
binance-futures extractor** (data-manager itself does not persist individual
trades — see `consumer/message_handler.py`). It had **no retention mechanism** and
grew unbounded at ~54k docs / ~22 MB per day. On 2026-07-01 it reached
**870,146 docs / ~349 MB dataSize**, pushing `petrosa_data_manager` logical size
over the **512 MB Atlas M0 cap** and blocking **all** cluster writes:

- distributed-lock Mongo init failing
- data-manager returning 500 on writes
- tradeengine boot probe failing → pod cycling (trading effectively halted;
  this is what had blocked petrosa-tradeengine#490 AC5)

The manual stopgap deleted `trades` older than 7 days (460,300 docs, ~184 MB
freed). At ~22 MB/day the collection would re-breach the cap in ~1.5 weeks
without a retention job.

## Why not a TTL index

`trades.timestamp` (and `trade_time`, `extracted_at`) are stored as **ISO-8601
strings**, not BSON `Date` values. A native MongoDB TTL index requires a `Date`
field, so TTL is impossible on the current schema — the same root pattern as the
`intents` bug (#244).

## The fix

A scheduled maintenance job — `data_manager.maintenance.trades_retention` —
deletes documents older than a configurable window via **lexicographic string
comparison** on the ISO-8601 `timestamp` field. ISO-8601 timestamps sort
lexicographically exactly as they sort chronologically, so
`{"timestamp": {"$lt": cutoff_iso}}` selects precisely the stale rows (mirroring
the manual remediation). Deletion walks **oldest-first in bounded batches** so a
single run's blast radius is capped and a large backlog is reclaimed over
successive runs.

| Property              | Value                                              |
| --------------------- | -------------------------------------------------- |
| Collection            | `trades` (`TRADES_RETENTION_COLLECTION`)           |
| Comparison field      | `timestamp` string (`TRADES_RETENTION_TS_FIELD`)   |
| Retention window      | 7 days (`TRADES_RETENTION_DAYS`)                   |
| Batch size            | 5000 docs (`TRADES_RETENTION_BATCH_SIZE`)          |
| Max batches per run   | 400 (`TRADES_RETENTION_MAX_BATCHES`)               |
| Database              | `petrosa_data_manager` (explicit; `MONGODB_DATABASE`) |

The cutoff string is rendered without a timezone suffix (`%Y-%m-%dT%H:%M:%S`) so
it is a clean lexicographic lower bound: a stored value like
`2026-07-01T00:00:00Z` sharing the same second-prefix is *longer* and therefore
compares greater, so boundary docs are conservatively **kept**, never deleted
early.

## Running it

```bash
# Dry-run — report how many docs would be deleted, mutate nothing.
opentelemetry-instrument python -m data_manager.maintenance.trades_retention --dry-run

# Apply — delete stale trades in bounded, oldest-first batches.
opentelemetry-instrument python -m data_manager.maintenance.trades_retention --apply
```

Exit codes: `0` success, `2` `MONGODB_URL` not set, `4` MongoDB error.

## Deployment

This module is the **logic** half of the retention path. The scheduled
**CronJob** manifest lives in `petrosa_k8s` (mirroring the klines-retention and
health-metrics-retention CronJobs) and is tracked as a companion infra ticket.
Recommended schedule: hourly, so the collection never drifts far above steady
state between runs.

## Long-term option (AC4)

A better long-term fix is to store a real BSON `Date` field on `trades` at ingest
(in the extractor) so a native TTL index can replace this scheduled job. That is
a cross-repo change requiring extractor coordination and is deliberately left as
a follow-up; the scheduled job is the safe, self-contained fix that stops the
recurring P0 now.

## Related

- data-manager#244 — intents TTL fix (prior culprit; same string-timestamp root pattern)
- petrosa_k8s#905 — Grafana leading-indicator alert for Atlas data-size (would warn before write-blocking)
- petrosa-tradeengine#490 / #493 — the AC5 this P0 had blocked
- Prior Atlas P0s: 2026-06-10 / 06-16 / 06-19 (all `intents`); 2026-07-01 (`trades`, this ticket)
