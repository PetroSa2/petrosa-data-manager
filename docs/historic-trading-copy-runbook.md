# Historic trading copy runbook

`copy_historic_trading_history` copies the MongoDB `execution_events`, `trades`,
and symbol-suffixed `trades_*` collections into the durable MySQL tables.

The command is a dry run unless `--apply` is supplied. A dry run reports rows
per UTC day and invalid documents without changing either database.

```bash
uv run python -m data_manager.maintenance.copy_historic_trading_history \
  --batch-size 500 \
  --checkpoint /tmp/trading-history-copy.json
```

Review the per-day output, then repeat with `--apply`. The checkpoint is
updated after each applied batch and can be used to resume an interrupted run.
Use `--collection execution_events` or `--collection trades` to run one group.

The MySQL writes use the existing durable-table unique keys, so rerunning a
completed range is idempotent. After the copy, run the proof for each group:

```bash
uv run python -m data_manager.maintenance.historic_copy_proof \
  --collection execution_events
uv run python -m data_manager.maintenance.historic_copy_proof \
  --collection trades
```

The proof must return an empty `failures` list for both collections before any
retention policy is enabled.

## Typed Trading-State Recovery

The `/api/v1/trading` positions and daily-P&L writers store private
`_mysql_copy_pending`, `_mysql_copy_version`, and `_mysql_copy_attempted_at`
fields atomically with each Mongo mutation when the existing MySQL copy flag is enabled.
The Mongo document is the durable typed-state recovery payload, identified by
its collection and immutable position ID or date. Updates coalesce into the latest typed state;
this is a state projection, not a new per-operation event archive.
Typed reads omit these internal fields, and clients cannot set them.
Mismatched identities in typed PUT, PATCH, close, or close-by-side updates return 409.
The Mongo `_id` is private; an existing `id` cannot be changed by typed updates.

`trading_state_mysql_sequences` is a durable Mongo control catalog keyed by
collection and logical ID, using Mongo's built-in unique `_id` index.
Each mutation reserves a monotonically increasing version before the atomic state
write, then conditionally writes only if no newer source version already won.
A reversed allocation/write race returns 409 rather than overwriting newer state.
Unused sequence numbers from failures or duplicate creates are harmless gaps.
The catalog survives source-document recreation and process restart; do not reset,
delete, or expire its rows. It is control metadata, not a second state/event archive.

The API lifespan polls every five seconds and requests can wake one local worker.
Each pass reads at most 25 pending documents per table, processes one at a time,
and makes at most three copy attempts per document with bounded backoff.
SQL runs off the serving event loop. Failed documents rotate behind other pending
work using their last-attempt time; they remain queryable across process restarts.
The existing positions and daily-P&L collections have no TTL; this change adds
pending-scan indexes but does not add or change retention policies.

Replay locks the target row using SQL's actual equality and compares its stored
identity exactly with the requested one before consulting a cursor.
Case-, accent-, or space-equivalent aliases under that target's collation are
rejected and remain pending in Mongo; no production collation is assumed.
MySQL `trading_state_mysql_versions` then serializes each exact key with a row lock.
The typed-owned target fields and committed version change in one InnoDB transaction.
A repeated or older version has no effect. Mongo acknowledges only the version
that committed, so concurrent newer writes cannot lose their pending marker.
A committed cursor whose target row is missing is an error, not a successful no-op.
Existing SQL primary IDs cannot change. For a SQL position marked `superseded`,
the ledger owns `status`, `close_reason`, and `pnl_unknown`; replay preserves all three
even when stale Mongo values differ, while applying typed-owned fields such as quantity.
The target row lock serializes this rule with the ledger's supersede transaction.
Mongo is not universally authoritative over shared SQL rows; other generic or external
writers do not acquire replay versions, and their ownership must not be inferred from this path.

MySQL dead letters are diagnostic copies, not the recovery source. Missing tables,
open circuits, or failed diagnostic writes do not discard Mongo pending state.
`GET /api/v1/trading/mysql-copy/status` reports durable pending documents separately
from process-local task count, copy failures, confirmed dead letters, fallback
failures, task failures, and replay successes. `trading_state_recovery_events_total`
exports bounded `outcome` values: `fallback_failed`, `task_failed`, and `replayed`.
Existing copy-failure and dead-letter metrics remain available. Process counters
reset on restart; durable pending counts are read from Mongo, not task memory.

### Schema Ownership And Upgrade

Data-manager owns `trading_state_mysql_dead_letters` and
`trading_state_mysql_versions`. Its existing `metadata.create_all()` bootstrap
creates missing tables on MySQL connection; it does not alter existing tables
or remove existing rows. The standalone operator migration
`data_manager/scripts/migrations/019_trading_state_recovery.sql` provides the same
additive `CREATE TABLE IF NOT EXISTS` path for MySQL 5.7 upgrades.
Do not run the whole legacy migration 013 for this recovery upgrade: it includes
unrelated ALTER statements, including syntax not supported by MySQL 5.7.

Before an authorized rollout, review both existing table definitions and confirm
the target positions/daily-P&L tables are InnoDB and have their documented unique
logical keys. An incompatible existing schema requires a separately reviewed
forward repair; `CREATE TABLE IF NOT EXISTS` does not repair incompatible columns.
Never drop or truncate either recovery table or pending Mongo state to roll back.
If application code must roll back, leave additive tables and private Mongo fields
intact. Restore the fixed worker to resume; old code cannot replay pending markers.
Never reset either the Mongo sequence catalog or committed MySQL versions while
recovery work or retained target rows can exist.

Deployment, production DDL, replay/reconciliation, and controlled outages require
the operator's separate authorization. Offline fixture results do not establish
production schema provisioning, historical completeness, or absence of data loss.
After an authorized rollout, retain image/commit identity and contiguous two-hour
UTC log/status bounds, source coverage, copy/fallback/task failure counts, durable
pending counts, and replay outcomes. No natural outage means restart recovery was
not exercised in production; do not infer it from an absence of sampled errors.
