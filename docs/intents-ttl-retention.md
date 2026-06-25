# Intents TTL Retention (data-manager#244)

Root-cause fix for three MongoDB Atlas M0 quota P0 incidents
(2026-06-10, 2026-06-16, 2026-06-19) caused by the `intents` audit-trail
collection growing without a working TTL index until it exhausted the 512 MB
Atlas quota and halted **all** cluster writes.

## What was broken

`petrosa_k8s#820` (and the `#884` collMod follow-up) tried to put a TTL index on
the `intents` collection keyed on `timestamp`. Two compounding defects:

1. **Name collision.** The application creates a *plain* `timestamp_1` index on
   the `intents` collection on every consumer startup
   (`MongoDBAdapter.ensure_indexes`). A TTL index reusing the name `timestamp_1`
   collides (`IndexOptionsConflict`), so the TTL was silently lost on the next
   deploy.
2. **Wrong field.** `timestamp` is publisher-supplied and the ticket body even
   referred to a non-existent `createdAt` field. Neither is a reliable,
   data-manager-controlled BSON `Date`.

Live evidence (2026-06-19): 411,413 intents, 100% older than 1 day, and
`timestamp_1` present **without** `expireAfterSeconds`.

## The fix

A TTL index under a **dedicated name** on the **subscriber-set** field:

| Property              | Value                                            |
| --------------------- | ------------------------------------------------ |
| Index name            | `received_at_ttl_1d`                              |
| Key                   | `{ received_at: 1 }`                              |
| `expireAfterSeconds`  | `86400` (1 day; `MONGODB_INTENTS_TTL_SECONDS`)   |
| Database              | `petrosa_data_manager` (explicit)                |

`received_at` is always written by data-manager as a real `datetime`
(`IntentEvent.received_at`, `default_factory=datetime.now(UTC)`), so the TTL
monitor actually purges. The dedicated name never collides with the app-managed
`timestamp_1` index.

At ~280k docs/day a 1-day window holds the collection at ~280 MB (~45% headroom
under the 512 MB Atlas M0 quota).

### Two layers of defense

1. **Self-heal (primary).** `MongoDBAdapter.ensure_indexes("intents")` now
   declares `received_at_ttl_1d`, so the index is recreated on every consumer
   startup even if an operator drops it. No manual step required after a deploy.
2. **Standalone job (operator-runnable, auditable).**
   `data_manager.maintenance.intents_ttl_index` creates/repairs the index on
   demand and audits sibling collections.

## Environment contract (AC4)

| Variable                      | Default                | Meaning                                                        |
| ----------------------------- | ---------------------- | -------------------------------------------------------------- |
| `MONGODB_URL`                 | — (required)           | Full connection string.                                        |
| `MONGODB_DATABASE`            | falls back ↓           | **Explicit** target DB name (AC4).                             |
| `MONGODB_DB`                  | `petrosa_data_manager` | Repo-convention fallback when `MONGODB_DATABASE` is unset.     |
| `MONGODB_INTENTS_TTL_SECONDS` | `86400`                | TTL window in seconds (min 60).                                |

The job resolves the database name as
`MONGODB_DATABASE → MONGODB_DB → "petrosa_data_manager"` and selects it
**explicitly** — it never derives the DB from the connection-string path, which
is exactly how `#820` ended up writing to the wrong database.

## Running the job

```bash
# Dry-run — report planned changes, mutate nothing.
opentelemetry-instrument python -m \
    data_manager.maintenance.intents_ttl_index --dry-run

# Apply — create/repair the TTL index and drop legacy broken indexes.
opentelemetry-instrument python -m \
    data_manager.maintenance.intents_ttl_index --apply
```

Exit codes: `0` success · `2` `MONGODB_URL` not set · `4` MongoDB error.

The job logs a single auditable INFO line per run with the database, collection,
index spec, action taken (`created` / `noop` / `collmod` / `recreated`), and any
legacy `createdAt`-keyed TTL indexes it dropped (AC2/AC3). Confirm purging is
actually happening — do **not** assume from index presence alone (this is the
verification step `#820` skipped):

```bash
mongosh "$MONGODB_URL" --eval \
  'db.getSiblingDB("petrosa_data_manager").intents.countDocuments({})'
# Re-check after >1 day; the count must stop growing without bound.
```

## Sibling-collection retention decisions (AC5)

The job audits these sibling collections every run and logs their current
TTL-index state:

| Collection         | Decision                                                    |
| ------------------ | ----------------------------------------------------------- |
| `alerts`           | Retain — not auto-expired pending volume evidence.          |
| `cio_decisions`    | Retain — financial/audit trail; not auto-expired.           |
| `execution_events` | Retain — financial/audit trail; not auto-expired.           |
| `pnl_events`       | Retain — audit trail (watch: repeated mark-to-market rows). |
| `trades`           | Retain — financial/audit trail; not auto-expired.           |

**Rationale.** Only `intents` has demonstrated unbounded growth that breaches
the quota (~280k docs/day, dominated by pre-CIO and multi-intent-per-decision
volume). The siblings are audit/financial-event trails with material value and
no current evidence of runaway growth, so auto-expiry is **deliberately not**
applied to them yet. The leading-indicator Grafana alert (AC6, in
`petrosa_k8s`) — WARN at `data_size / quota > 0.7`, P1 at `> 0.85` — is the
safety net that surfaces any sibling that starts to threaten the quota, at which
point its row in this table is flipped to a managed TTL (add it to the job's
managed set with its documented timestamp field).

## Related

- `petrosa_k8s#820` — original TTL job (shipped, broken: name collision + wrong field).
- `petrosa_k8s#884` / `#887` — collMod 7d→1d follow-up (also broken; superseded).
- `docs/klines-retention.md` — sibling retention job (MySQL klines).
