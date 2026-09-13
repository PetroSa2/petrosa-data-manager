# AC1 Evidence — Stale Per-Symbol Mongo Collections Re-audit

**Ticket:** [`PetroSa2/petrosa-data-manager#273`](https://github.com/PetroSa2/petrosa-data-manager/issues/273)
**Related:** [`PetroSa2/petrosa-data-manager#272`](https://github.com/PetroSa2/petrosa-data-manager/issues/272) / PR #288 (this doc + module mirror that PR's approach)
**Parent epic:** `PetroSa2/petrosa_k8s#783` (Atlas quota P0 series)
**Generated:** 2026-09-13

## Root cause — corrected (per the issue's own 2026-09-13 adversarial-review comment)

The issue as originally filed claimed `trades_{symbol}` has "no API reader" and that the
collections were "leftovers from an early `petrosa-socket-client` experiment". Both claims are
corrected by the issue's own follow-up comment, verified again here against repo HEAD:

- **`trades_{symbol}` has a live API reader.** `GET /api/v1/data/trades`
  (`data_manager/api/routes/data.py:279`) instantiates `TradeRepository` and calls
  `get_range(pair, start, end)` (`data.py:314` → `trade_repository.py:70-89`), which reads
  `trades_{symbol}`. `tickers_{symbol}` genuinely has **no** reader — `TickerRepository` is
  defined and exported but never instantiated by any route (`api/routes/` grep: only
  `TradeRepository` is wired).
- **The dormant writer is this repo's own code**, not an external service: `TradeRepository`/
  `TickerRepository` (`trade_repository.py:17-33`, `ticker_repository.py:16-32`) both still
  define working `insert`/`insert_batch` methods that would write to `trades_{symbol}`/
  `tickers_{symbol}` — but nothing calls them. `message_handler.py:112` increments an in-memory
  `trades` stat counter and does **not** persist to Mongo; the persistence call site was removed
  2025-10-21 (`f862a9a`, "remove raw data persistence").

## AC1 — Per-collection re-verification (2026-09-13)

| Collection | Docs (2026-09-02 audit) | Writer wired? | Reader wired? | Verdict |
| --- | --- | --- | --- | --- |
| `tickers_BTCUSDT` / `tickers_ETHUSDT` | 89 each | none (dormant since 2025-10-21) | **none** | **drop** (default target) |
| `trades_BTCUSDT` / `trades_ETHUSDT` | 824 / 822 | none (dormant since 2025-10-21) | **yes** — `GET /api/v1/data/trades` | **retain by default**; drop only with explicit operator opt-in (see below) |
| `trades` (plain, no suffix) | 53,825 (9.36 MB) | none confirmed | none confirmed | **drop** (default target — retirement already decided, see below) |

Note on the `trades_{symbol}` reader in practice: `GET /api/v1/data/trades` defaults its query
window to `[now-1h, now)` when the caller omits `start`/`end` (`data.py:309-312`). Since the
newest document in either collection is from 2025-10-21, the endpoint already returns an empty
list for the overwhelmingly common (no-explicit-range) call shape today — the "wired reader"
caveat matters only for callers who pass an explicit historical `start`/`end` covering
2025-10-21.

## AC1 — `trades` (plain) retirement is a pre-existing decision, not new scope

`data_manager/maintenance/intents_ttl_index.py`'s `SIBLING_COLLECTIONS` audit list already
carries this note (added prior to this ticket, unchanged by it):

> "The `trades` override was removed with the trades retention job
> (`data-manager#254`): the `trades` collection is being retired entirely
> (`binance-data-extractor#276`), so it now falls back to the default sibling decision until the
> collection is dropped."

This ticket's AC3 ("determine whether the plain `trades` collection has any consumer") is
therefore already answered by that prior decision: no consumer, retirement already decided, this
migration is the mechanical follow-through.

## AC2/AC3 — Guarded drop migration

`data_manager/maintenance/drop_stale_market_data_collections_2026_09.py` mirrors
`drop_orphan_petrosa_crypto_tables_2026_09.py` (#272/PR #288): `--dry-run` / `--apply`, per-
collection independent guard, never fatal to the batch, idempotent drop.

One structural difference from the MySQL script: MySQL orphan tables were verified **0-row**
before the guard could ever matter, so #288 used a **row-count** guard (any row = abort that
table). These Mongo collections hold real historical documents (89–53,825 depending on
collection), so the guard here is **recency-based** instead: a collection is only dropped in
`--apply` mode if its newest document is older than `--min-age-days`
(`MARKET_DATA_STALE_MIN_AGE_DAYS`, default 30). A collection that received a write more recently
than the guard window indicates a writer reactivated since this audit — the drop is skipped
(not fatal to the batch) and reported for re-audit, exactly mirroring #288's "guard tripped,
skip, not fatal" behavior.

A second guard layer specific to this ticket: `trades_{symbol}` (the wired-reader category) is
excluded from `--apply` **entirely** unless the operator passes `--include-wired-reader`,
independent of the recency guard. This mirrors #272/PR #288's AC5 precedent of retaining
wired-but-empty-reader tables (`datasets`/`lineage_records`) rather than auto-including them in
a blanket drop.

Target discovery auto-scans `tickers_*` / `trades_*` / plain `trades` via
`MongoDBAdapter.list_collections()` rather than hardcoding `BTCUSDT`/`ETHUSDT`, so any other
per-symbol collection matching the same dead shape (e.g. a hypothetical `tickers_SOLUSDT`) is
picked up automatically rather than requiring a script edit.

**No destructive operation was executed against any live database as part of this ticket.** The
script is delivered as an operator-invoked tool; running it against staging/prod is the AC4
operational step below.

## AC4 — Post-drop verification runbook (operator-executed, not run here)

1. Run `--dry-run` against staging first. Confirm:
   - `tickers_BTCUSDT` / `tickers_ETHUSDT` (or whatever `tickers_*` exist) report a newest-doc
     age comfortably past `--min-age-days` and would be dropped.
   - `trades` (plain) reports the same.
   - `trades_{symbol}` collections are reported as `retained_wired_reader` (not touched) unless
     `--include-wired-reader` was passed.
2. Run `--apply` against staging (without `--include-wired-reader` for the first pass). Confirm
   via `db.getCollectionNames()` / `collStats` that `tickers_*` and `trades` are gone,
   `trades_{symbol}` remain.
3. Restart the data-manager pod in staging; confirm no errors reference the dropped collections
   (expected: none, since AC1 confirmed zero readers for `tickers_*`; `trades` has none
   confirmed either).
4. Watch `GET /api/v1/data/trades` behavior for 24h in staging — it should return an empty list
   for `BTCUSDT`/`ETHUSDT` exactly as it already effectively does today for the default 1h
   window; confirm no 5xx.
5. Repeat steps 1–3 against prod once staging is clean; monitor Atlas data-size metric for the
   expected reclaim (~0.25 MB total across the four per-symbol collections + ~9.36 MB for plain
   `trades`).
6. `trades_{symbol}` drop decision (separate, deliberate step): once product/ops confirms
   `GET /api/v1/data/trades` returning empty for these symbols is acceptable (or the route is
   deprecated), re-run `--apply --include-wired-reader --collection trades_BTCUSDT --collection
   trades_ETHUSDT` to complete the family. Not bundled into the default run by this ticket.

## Refs

- Implementation module: `data_manager/maintenance/drop_stale_market_data_collections_2026_09.py`
- Unit tests: `tests/test_drop_stale_market_data_collections_2026_09.py`
- Prior pattern: `data_manager/maintenance/drop_orphan_petrosa_crypto_tables_2026_09.py` (#272 / PR #288)
- `trades` retirement decision origin: `data_manager/maintenance/intents_ttl_index.py`
  (`SIBLING_DECISION_OVERRIDES` history comment), `data-manager#254`, `petrosa-binance-data-extractor#276`
- Parent epic: `PetroSa2/petrosa_k8s#783`
