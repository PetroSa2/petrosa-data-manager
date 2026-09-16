# Readerless Growing Collections — 2026-09-15 Audit — Decision Record

**Ticket:** [`PetroSa2/petrosa-data-manager#302`](https://github.com/PetroSa2/petrosa-data-manager/issues/302)
**Related:** #271 (alerts TTL + kill-switch), #267 (signals TTL, mandatory-consumer follow-up),
#272/PR #288, #273/PR #289, #301/PR #307 (guarded-tooling precedent), #274/#275 (candle-store
cutover, `docs/candle-cutover-runbook.md`), petrosa-bot-ta-analysis#284 (raw-MySQL fallback
removed, merged 2026-09-15)
**Generated:** 2026-09-15/16, live-verified via read-only queries against production Atlas +
DBaaS MySQL (no writes/DDL executed as part of this audit).

**No destructive operation was executed against any live database as part of this ticket.**
Every code change below is either a reversible kill-switch (env var, default preserves current
behavior) or an idempotent, operator-invoked TTL-index maintenance job (`--dry-run`/`--apply`,
mirroring #272/#273/#301's guarded pattern). No collection or table is dropped.

## Summary of corrections to the issue's stated evidence

The issue's premise ("no reader anywhere," fixed row/doc counts) does not survive re-verification
against source and live data, in the same pattern #272/#273/#301 established for this repo:

| Claim in issue | Live/source re-verification | Correction |
| --- | --- | --- |
| `config_rate_limits`: "NO reader in any repo" | `petrosa_otel.ConfigRateLimiter.check_rate_limit` (`rate_limiter.py:192`, `coll.find(query)`) reads it for sliding-window quota checks; wired into 4 services | **Has a confirmed reader.** Root cause is that its documented TTL index was never applied, not absence of a consumer. |
| `signals` MySQL: "44507 rows" | `SELECT COUNT(*) FROM petrosa_crypto.signals` → **14,380,009 rows** (2026-09-16 live query) | The issue's figure came from `information_schema.tables.table_rows`, InnoDB's notoriously stale estimate — off by ~323x. This is real historical trading-signal data, not a small duplicate. |
| `signals`: implied simple Mongo-vs-MySQL duplication | `petrosa-bot-ta-analysis#284` (merged 2026-09-15, independent ticket) removed the raw-pymysql fallback entirely; `MySQLClient(use_data_manager=False)` now raises `RuntimeError` | MySQL `signals` writer is **already dead** as of today — not something this ticket needs to kill. The "duplication" was a live-vs-legacy transition artifact, now resolved on the write side by a sibling ticket. |
| `alerts`: implied unaddressed | `alert_dispatcher.py`/`intents_ttl_index.py` already ship a TTL index (`_ttl_inserted_at_ttl`, 604800s) + kill-switch (`PETROSA_ALERT_PERSIST_ENABLED`) per #271 | Already compliant; live-verified the TTL index **is** applied in production (`_ttl_inserted_at_ttl`, `expireAfterSeconds: 604800`). |

## Per-collection decision

### 1. `alerts` — **RETAIN, no code change**

- Writer: `data_manager/services/alert_dispatcher.py:438` (`replace_one` upsert into `ALERTS_COLLECTION`).
- Reader: none confirmed in the 8-repo ecosystem (re-verified 2026-09-15/16; unchanged since #271).
- TTL: `data_manager/maintenance/intents_ttl_index.py:450` `ensure_alerts_ttl_index` — **live-verified
  applied** (`_ttl_inserted_at_ttl`, `expireAfterSeconds=604800`, live count 86,140 docs 2026-09-16).
- Kill-switch: `alert_dispatcher.py:82` `_persist_enabled()` — `PETROSA_ALERT_PERSIST_ENABLED`
  (default `true`).
- **Decision:** retain as a bounded, reversible, write-only audit trail. Whether to build a
  reader (dashboard / CIO review path per the issue's own suggestion) is a genuine product
  decision, not resolved here — documented as an **open follow-up**, not a blocker. Default
  safe choice already in place: TTL-capped, instantly reversible via the kill-switch.

### 2. `config_rate_limits` — **RETAIN + CORRECT + close the TTL/kill-switch gap**

- Writer + Reader: `petrosa_otel.rate_limiter.ConfigRateLimiter` (vendored from
  `petrosa_k8s/templates/shared/middleware/rate_limiter.py`) — `check_rate_limit()`
  (`rate_limiter.py:192`, `coll.find(query)`) performs the sliding-window quota read; every write
  goes through the same class. Wired into:
  - `petrosa-tradeengine/tradeengine/api.py:129-136`
  - `petrosa-bot-ta-analysis/ta_bot/main.py:123-130`
  - `petrosa-data-manager/data_manager/main.py:553-586` (this repo)
  - `petrosa-realtime-strategies/strategies/main.py:136-147`
- Database: lives in the **shared** `petrosa` Atlas database
  (`petrosa_k8s/k8s/shared/configmaps/petrosa-common-config.yaml:80`
  `MONGODB_DATABASE: "petrosa"`) — **not** this repo's own `petrosa_data_manager` (which is
  reached via this repo's dedicated `MONGODB_URL` secret, a different connection string/path).
- TTL: `petrosa_k8s/scripts/mongodb/init-rate-limiting.js` documents a 1-hour TTL on `timestamp`.
  **Live-verified NOT applied**: as of 2026-09-16, `petrosa.config_rate_limits` has only the
  default `_id_` index; oldest live document dates to **2026-03-13** (over 6 months of
  unbounded accumulation); live count 10,427 docs.
- **This is the corrected root cause of the "growing" symptom** — a confirmed consumer exists,
  but the documented retention mechanism was never actually run against the live cluster.
- **Action taken (this repo's scope):**
  - `data_manager/maintenance/intents_ttl_index.py`: new
    `ensure_config_rate_limits_ttl_index()` (idempotent create/collmod/noop, mirrors
    `ensure_alerts_ttl_index`), targeting the shared `petrosa` database explicitly
    (`MONGODB_CONFIG_RATE_LIMITS_DATABASE`, default `"petrosa"`) — NOT auto-executed; operator
    runs `python -m data_manager.maintenance.intents_ttl_index --apply` (or
    `--dry-run` first) to actually create the index, consistent with #272/#273/#301's
    guarded-tooling-only precedent.
  - `data_manager/main.py:558-586`: new `CONFIG_RATE_LIMIT_ENABLED` env kill-switch
    (default `true`) wired to `ConfigRateLimiter(..., enabled=...)` for this service's own
    instantiation.
- **Cross-repo follow-up (flagged for the operator, not filed as a new ticket, per #301's
  precedent):** the other three services (tradeengine, ta-bot, realtime-strategies) do not yet
  wire an equivalent env kill-switch, and none of the four services' startup paths actually run
  `init-rate-limiting.js` or an idempotent self-heal — someone needs to either run the `--apply`
  command above once (against the shared `petrosa` DB) or add it to a scheduled job. This
  ticket closes the gap this repo owns; it does not unilaterally fix shared infra it doesn't
  own outright.

### 3. `signals` (Mongo) — **RETAIN (sole live writer) + close the TTL/kill-switch gap**

- Writer: `data_manager/api/routes/generic.py` `insert_records`, reached via
  `ta_bot/services/data_manager_client.py:238-297` `persist_signal`/`persist_signals_batch`
  (`POST /api/v1/mongodb/signals`). Confirmed **sole active writer** as of 2026-09-15 (the MySQL
  path is dead — see #4 below).
- Reader: none confirmed anywhere in the 8-repo ecosystem (re-checked 2026-09-15/16, including
  `petrosa-cio` — its `signals` references are the unrelated `MarketSignals` domain model, not
  this collection — and `tradeengine/services/metrics_aggregator.py:263`'s `get_success_rates`,
  which is dead `TODO`/mock-data code, not a real query).
- TTL: `generic.py:389` already stamps a dedicated `_ttl_inserted_at` BSON Date field on every
  `signals` document, and `intents_ttl_index.py:364` `ensure_signals_ttl_index()` already exists
  to index it. **Live-verified NOT applied**: as of 2026-09-16, `petrosa_data_manager.signals`
  has only the default `_id_` index despite every document already carrying the stamp field —
  the ensure-function was written (companion to #267 AC6) but never actually run against the
  live cluster. Live count: 204 docs.
- Kill-switch: **missing** prior to this change (unlike `alerts`).
- **Action taken:**
  - `data_manager/api/routes/generic.py`: new `_signals_persist_enabled()` helper +
    gate in `insert_records`, mirroring `alert_dispatcher._persist_enabled()`
    (`PETROSA_SIGNALS_PERSIST_ENABLED`, default `true`). Scoped to `mongodb.signals` only —
    verified by test not to affect `alerts` or the legacy `mysql.signals` path.
  - The TTL-index fix already exists in code (`ensure_signals_ttl_index`); operator runs
    `python -m data_manager.maintenance.intents_ttl_index --apply` to actually create it
    (same guarded-tooling precedent — not executed by this ticket).
- **Decision:** retain (it is the sole surviving persisted signal history going forward); no
  confirmed reader remains an **open product decision** (build a consumer vs. formally sunset)
  — defaulted to the safest reversible choice (retain, TTL-ready, kill-switch added) rather than
  dropping data, per this ticket's operating instructions.

### 4. `signals` (MySQL, `petrosa_crypto.signals`) — **RETAIN, no drop tooling shipped**

- Writer: **confirmed dead** as of `petrosa-bot-ta-analysis#284` (merged 2026-09-15T21:36:27Z,
  commit `057013e`) — the raw-pymysql fallback branches (`connect`/`fetch_candles`/
  `persist_signal`/`persist_signals_batch`) were deleted; `MySQLClient(use_data_manager=False)`
  now raises `RuntimeError` instead of opening a direct connection. No code path in the
  ecosystem can write to this table anymore.
- **Row count correction:** the issue states 44,507 rows (an `information_schema.tables`
  estimate). A live `SELECT COUNT(*)` on 2026-09-16 returned **14,380,009 rows** — genuine
  historical trading-signal data spanning the service's operating history, not a small
  duplicate of the 204-doc Mongo collection.
- **Decision: do NOT ship a drop tool for this table.** Given the true scale (14.38M rows) and
  that this is likely the ecosystem's primary historical signal record, dropping it would be an
  irreversible loss of significant data — exactly the case this ticket's operating instructions
  flag as requiring a genuine human product call, not a bot decision. The safest reversible
  choice is retain-as-is: no further writes are possible (writer already removed), so there is
  no ongoing growth risk to mitigate.
- **Open follow-up for a human decision (not resolved here):** whether this table warrants a
  retention/archival policy given its size on the shared MySQL DBaaS, and whether it should be
  exported/archived before any future drop is even considered. This is explicitly parked, not
  blocking this ticket's completion.

## Live verification queries (read-only, 2026-09-16)

```text
petrosa_data_manager.alerts            count=86140  indexes={_id_, timestamp_1,
                                        symbol_1_timestamp_1, _ttl_inserted_at_ttl(604800s)}
petrosa_data_manager.signals           count=204    indexes={_id_}  (no TTL index — gap closed
                                        by this PR's new ensure fn; operator must --apply)
petrosa.config_rate_limits             count=10427  indexes={_id_}  (no TTL index — oldest doc
                                        2026-03-13; gap closed by this PR's new ensure fn)
petrosa_crypto.signals (MySQL)         COUNT(*)=14380009  (information_schema estimate: 44507 —
                                        stale by ~323x)  newest created_at=2026-09-11 21:11:13
```

## Acceptance-criteria mapping

- **AC1** (decision comment per collection, with evidence): posted to the issue; mirrored above.
- **AC2** (kept collections have documented reader/TTL/kill-switch cited by file:line): satisfied
  for `alerts` (pre-existing) and `config_rate_limits`/`signals` (this PR closes the TTL/
  kill-switch gaps; readers cited above for `config_rate_limits`; `alerts`/`signals` readers
  remain an explicitly documented open decision, not a blocker per this ticket's operating
  instructions).
- **AC3** (`signals` duplication resolved to a single store, before/after counts pasted):
  **partially superseded by corrected evidence** — the MySQL side's writer was already killed by
  an independent ticket (#284) before this ticket ran, and the true MySQL row count (14.38M) rules
  out treating it as a droppable duplicate. Both stores are retained; before-counts are pasted
  above; no after-counts because no live DDL/drop was executed (by design).
- **AC4** (`config_rate_limits` writer identified by file:line): done — see table above; not
  orphaned, so the write is not stopped.
- **AC5** (live Atlas re-query confirms final state, pasted in a comment): done — see verification
  block above and the issue comment.
