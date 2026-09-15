# Atlas Hygiene — 2026-09-14 Audit — Decision Record

**Ticket:** [`PetroSa2/petrosa-data-manager#301`](https://github.com/PetroSa2/petrosa-data-manager/issues/301)
**Related:** #272/PR #288 (MySQL orphans), #273/PR #289 (stale per-symbol Mongo collections),
#274/#275/PR #287 (candle-store cutover), #282 (generic query API), #283 (klines_1d staleness),
#302 (sibling — readerless *growing* collections, explicitly out of scope here)
**Parent epic:** `PetroSa2/petrosa_k8s#783` (Atlas quota P0 series)
**Generated:** 2026-09-15

This ticket covers only the pieces of the 2026-09-14 audit that are genuinely zero-risk once
re-verified against repo HEAD (this repo) and `petrosa-tradeengine` HEAD. **No destructive
operation was executed against any live database as part of this ticket** — every drop/purge
below ships as an operator-invoked, guarded tool.

## Four-database inventory (final state / decision)

| Database | Audit finding | Decision | Rationale |
| --- | --- | --- | --- |
| `petrosa_data_manager` | 24 collections, 120.9 MB — the real app DB | keep, unchanged | primary store |
| `petrosa` | config collections, mostly 0-doc | keep DB; drop `_write_test_probe` only | see below |
| `binance` | 1 doc: the **active** leader-election record | **retain**, document as the designated leader-election DB | see "binance / leader-election" below |
| `mongodb` | 0-doc `tradeengine_boot_probes` copy | **retain the DB**; opt-in 0-doc drop of the collection only | see "mongodb DB correction" below |

No database is dropped by this ticket. `MONGODB_DB` is not changed.

## `binance` / leader-election — Option (b) selected

The issue's own 2026-09-14 correction comment identified two options:

- (a) point `MONGODB_DB` at `petrosa_data_manager`, migrate the live leader-election document,
  then drop `binance`; or
- (b) leave leader election where it is and document `binance` as the intended
  leader-election database.

**Option (b) is selected.** Option (a) requires a live migration of the pod's *active*
leader-election state (`data_manager/leader_election.py:56-64`, `scripts/
setup_leader_election.py:40,145`) plus a `MONGODB_DB` environment-variable change delivered via
`petrosa_k8s` manifests — a cross-repo, higher-blast-radius change touching a currently-correct-
functioning coordination mechanism, for a purely cosmetic naming improvement. Option (b) is
strictly safer and fully reversible: no data moves, no env var changes, no risk to the
distributed-lock mechanism that keeps only one pod running the auditor/analytics schedulers.

**Decision:** `binance` remains the leader-election database. `data_manager/leader_election.py`
and `scripts/setup_leader_election.py` are unchanged by this ticket. If a future ticket wants to
rename the connection default, it must do the live-migration dance from option (a) explicitly,
as its own deliberate, reviewed change — not bundled into a hygiene sweep.

## `mongodb` DB — correction (adversarial re-verification, 2026-09-15)

The issue (and its own correction comment) both state `mongodb` DB / its `tradeengine_boot_probes`
collection is "still a safe drop." Re-verified against `petrosa-tradeengine` HEAD, this is
**not quite right**:

- `tradeengine/services/data_manager_boot_probe.py:23` hardcodes `_PROBE_DB = "mongodb"` — has
  been the value since the probe's original commit (`8f1da7c`, tradeengine#451/#456,
  2026-06-05), unchanged through #465/#467/#468 (git blame confirmed).
- Every `petrosa-tradeengine` pod boot writes one sentinel document there, reads it back to gate
  `/readyz`, then a fire-and-forget 24h-TTL cleanup (`_cleanup_old_probes_sync`, `_TTL_HOURS = 24`)
  deletes stale docs.
- The audit's observed **0 docs** is the *expected steady state* of a working self-cleaning
  probe — not evidence the database or collection is abandoned.

**Decision:** the `mongodb` database is retained (dropping it is harmless in effect — Mongo
auto-recreates a DB/collection on the next insert — but mischaracterizes a live artifact as
dead, so we don't do it by default). `drop_stray_mongo_probes_2026_09.py` supports an explicit,
off-by-default `--include-boot-probe-artifacts` flag that:

1. 0-doc-guards a drop of the empty stray copies in `mongodb` and `petrosa` (safe: they're
   confirmed empty and Mongo recreates on next write with no behavior change to the probe).
2. TTL-guards a `delete_many` backlog purge of the `petrosa_data_manager` copy (1,550 residual
   docs per the audit), using the exact same filter shape tradeengine's own cleanup uses
   (`created_at < now - 24h`, ISO-8601 string comparison) so it never touches an in-flight or
   recent probe document.

**The actual cleanup-step bug (if the 1,550-doc backlog is still current) is
`petrosa-tradeengine`-owned code**, not this repo's. This PR does not and cannot fix
`data_manager_boot_probe.py`'s delete-step logic — that requires a `petrosa-tradeengine` ticket.
This module is a stopgap an operator can run without waiting on that cross-repo fix.

## MySQL `klines_*` — correction (new finding, not in the original audit)

The issue lists `klines_m1`, `klines_m3`, `klines_h2`, `klines_h4`, `klines_h6`, `klines_h8`,
`klines_h12` as 0-row MySQL orphans safe to drop. Re-verified against `data_manager/api/routes/
data.py` and `data_manager/db/repositories/candle_repository.py`:

- `GET /api/v1/data/candles` (`data.py:124-136`) accepts an **unrestricted** `period` query
  string — it is not validated against `constants.SUPPORTED_TIMEFRAMES`.
- `CandleRepository.get_range` → `_get_mysql_table_name(period)` (`candle_repository.py:55-62,
  93-98`) maps that caller-supplied string straight to `klines_{unit}{value}` whenever the
  MySQL backend serves the read (primary or #275 AC3 fallback).
- This is the exact "caller-supplied-suffix" shape #273 used to justify gating `trades_{symbol}`
  behind `--include-wired-reader` rather than calling it dead — the table name is directly
  reachable through a live, typed application code path, not merely the generic passthrough API.
- `klines_m1` / `klines_h4` additionally back two of `constants.SUPPORTED_TIMEFRAMES`
  (`1m`, `4h`) under the still-live candle-store cutover (#274/#275/PR #287); `
  CANDLE_DATABASE_TYPE` is deliberately not flipped to `mysql` in production yet, but the
  dual-write/fallback machinery is live infrastructure this ticket must not disturb.

**Decision:** none of the seven `klines_*` MySQL tables are added to any drop target in this or
any prior PR. They remain fully out of scope.

## `datasets` / `lineage_records` (MySQL) — reaffirmed, not reopened

Both were already explicitly retained by #272 (`docs/audit-orphan-tables-2026-09-13.md` AC5) as
a deliberate "latent/unpopulated catalog feature" decision — `datasets` has a genuine live
reader/writer (`CatalogRepository`, wired to `data_manager/api/routes/catalog.py`);
`lineage_records` has neither a reader nor a writer but was kept alongside `datasets` as part of
the same latent catalog feature rather than treated as orphan schema. The issue's body lists both
as orphans to drop; this ticket does **not** reopen that decision — it was a deliberate product
call, not a factual error, and reversing it is out of scope for a hygiene sweep. See the linked
doc for the original reasoning.

## Genuinely dead — new drop targets shipped in this PR

Confirmed zero references across all 8 ecosystem repos (`petrosa_k8s`, `petrosa-data-manager`,
`petrosa-tradeengine`, `petrosa-bot-ta-analysis`, `petrosa-binance-data-extractor`,
`petrosa-realtime-strategies`, `petrosa-socket-client`, `petrosa-cio`):

| Collection | DB | Docs (audit) | Verdict |
| --- | --- | --- | --- |
| `_atlas_quota_probe` | `petrosa_data_manager` | 0 | **drop** (default target) |
| `_p0_unblock_probe` | `petrosa_data_manager` | 0 | **drop** (default target) |
| `_write_test_probe` | `petrosa` | 0 | **drop** (default target) |

Shipped as `data_manager/maintenance/drop_stray_mongo_probes_2026_09.py` — `--dry-run`/`--apply`,
0-doc-guarded, idempotent, per-target independent (a guard trip on one never blocks the rest).
See the module docstring for the full target list including the opt-in boot-probe artifacts, and
`tests/test_drop_stray_mongo_probes_2026_09.py` for coverage.

## Already covered by prior PRs — no new code needed

- `tickers_BTCUSDT` / `tickers_ETHUSDT` / `trades_BTCUSDT` / `trades_ETHUSDT` / plain `trades`
  (Mongo) — `data_manager/maintenance/drop_stale_market_data_collections_2026_09.py` (#273/PR
  #289). Re-verified still valid; no changes made here.
- `strategy_positions`, `exchange_positions`, `position_contributions`, `extraction_metadata`,
  `trades` (MySQL), `funding_rates` (MySQL) —
  `data_manager/maintenance/drop_orphan_petrosa_crypto_tables_2026_09.py` (#272/PR #288).

## Explicitly out of scope (sibling tickets / prior decisions)

- `alerts`, `signals` (Mongo+MySQL), `config_rate_limits` — sibling #302 (readerless *growing*
  collections needing a consumer-or-drop decision). Not touched here.
- Mongo `klines_*` — active retention/consumer relationship, see #283. Not touched here.
- The 2026-09-01 execution/cio/pnl audit-write freeze — separate operational ticket
  cross-linking `petrosa_k8s#1057`.

## Operator runbook (not run as part of this ticket)

1. Run `--dry-run` against staging; confirm the three default probe targets show `doc_count: 0`
   and would be dropped.
2. Run `--apply` against staging (without `--include-boot-probe-artifacts`); confirm via
   `db.getCollectionNames()` that the three probes are gone.
3. Watch logs for 24h for any error referencing the dropped collections (expected: none — zero
   references confirmed in all 8 repos).
4. Repeat 1–2 against prod once staging is clean.
5. Separately, once satisfied the `petrosa_data_manager.tradeengine_boot_probes` backlog is still
   stale, run `--apply --include-boot-probe-artifacts` to flush it and the two empty stray
   copies. This does not require or wait on the `petrosa-tradeengine` root-cause fix.
6. File the `petrosa-tradeengine` ticket for the actual boot-probe cleanup-step bug (this repo
   cannot fix client-side cleanup logic in another repo).

## Refs

- Implementation module: `data_manager/maintenance/drop_stray_mongo_probes_2026_09.py`
- Unit tests: `tests/test_drop_stray_mongo_probes_2026_09.py`
- Prior pattern: `data_manager/maintenance/drop_orphan_petrosa_crypto_tables_2026_09.py` (#272),
  `data_manager/maintenance/drop_stale_market_data_collections_2026_09.py` (#273)
- Cross-repo evidence: `petrosa-tradeengine/tradeengine/services/data_manager_boot_probe.py`
- Parent epic: `PetroSa2/petrosa_k8s#783`
