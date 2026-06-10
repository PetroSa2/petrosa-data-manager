# AC1 Evidence — Orphan-Suspect Tables Re-classification

**Ticket:** [`PetroSa2/petrosa-data-manager#221`](https://github.com/PetroSa2/petrosa-data-manager/issues/221)
**Parent audit:** [`PetroSa2/petrosa_k8s#800`](https://github.com/PetroSa2/petrosa_k8s/issues/800)
**Audit report:** [`docs/storage-inventory-report-2026-06-06.md`](./storage-inventory-report-2026-06-06.md)
**Generated:** 2026-06-09

## Summary

The 2026-06-06 storage audit flagged four MySQL tables in `petrosa_crypto` as
`orphan-suspect` based solely on row count (`0`) and `update_time = NULL`. AC1
of #221 required tracing each table's writer in code to confirm or refute the
orphan classification.

**Result:** Only **1 of 4** tables is a confirmed orphan. The other 3 have
active code writers and are either dormant features (no CronJob deployed) or
behaviour anomalies (writer runs but data is empty).

## Methodology

Cross-repo grep for the table name across all Petrosa service repos in
write contexts (`INSERT INTO`, model classes, SQLAlchemy `Table()`
definitions, raw SQL DDL). Production status verified by checking
`petrosa_k8s/k8s/data-extractor/` for deployed CronJob manifests and by
inspecting whether the writer module is imported anywhere outside its own
tests.

## Per-table evidence

### `extraction_metadata` — NOT orphan

| Code reference | Path | Status |
| --- | --- | --- |
| Table definition | `petrosa-binance-data-extractor/db/mysql_adapter.py:177` | active |
| Writer abstraction | `petrosa-binance-data-extractor/db/base_adapter.py:199` (`write_extraction_metadata`) | active |
| Deployed CronJob | `petrosa_k8s/k8s/data-extractor/klines-extractor-cronjobs.yaml` | active (klines runs daily) |

**Why the table has 0 rows despite an active writer:** the klines extractor
runs daily and writes to MySQL klines tables (verified by audit: `klines_m5`
last update 2026-06-06), but the `extraction_metadata` row count remains 0.
This is a **behaviour anomaly** worthy of its own investigation, NOT an
orphan signal. Hypothesis: `write_extraction_metadata` may be guarded by an
adapter type check that disables the path under the current `DB_ADAPTER=mysql`
configuration, or the call site may have regressed. A follow-up ticket files
this as a code-level audit (see Follow-ups below).

**Verdict:** keep the table. Filing follow-up to investigate empty state.

### `funding_rates` — dormant feature, NOT orphan

| Code reference | Path | Status |
| --- | --- | --- |
| Table definition | `petrosa-binance-data-extractor/db/mysql_adapter.py:156` | active |
| Model class | `petrosa-binance-data-extractor/models/funding_rate.py:147` | active |
| Extraction job | `petrosa-binance-data-extractor/jobs/extract_funding.py:102` | active code |
| Deployed CronJob | `petrosa_k8s/k8s/data-extractor/` | **NOT deployed** (only klines cronjobs present) |

The writer exists and is fully wired up in `petrosa-binance-data-extractor`,
but no CronJob schedules the funding extractor in the current k8s manifests.
This is a **dormant feature** — the table backs a feature whose deploy was
either deferred or removed.

**Verdict:** keep the table. A drop here would commit to permanently
removing the funding-rates feature; that is a product decision. Filing
follow-up to ask operator to either revive the CronJob or formally retire
the feature (and only then drop the table).

### `position_contributions` — CONFIRMED ORPHAN

| Code reference | Path | Status |
| --- | --- | --- |
| Table DDL | `petrosa-tradeengine/scripts/create_strategy_positions_table.sql` | one-shot file, no runner |
| Writer module | `petrosa-tradeengine/tradeengine/strategy_position_manager.py:553+` | **DEAD CODE** |
| Production imports of `StrategyPositionManager` | _none_ | not referenced by any deployed code path |
| Tradeengine actual position writes | `petrosa-tradeengine/shared/mysql_client.py:create_position` writes to **`positions`** table only | live |
| data-manager refs to `position_contributions` | _none_ | not in this repo |

The table was created on 2026-03-04 by manually running the
`create_strategy_positions_table.sql` script (likely operator-applied; no
auto-runner in any repo). The Python writer in
`strategy_position_manager.py` exists but is referenced only by its own
unit-test file (`tests/test_strategy_position_manager.py`). Production
tradeengine writes positions to the `positions` table via
`shared/mysql_client.py:create_position`, **not** to `position_contributions`.
The 3-table architecture (`strategy_positions` / `position_contributions` /
`exchange_positions`) documented in `petrosa-tradeengine/docs/archive/2024-2025/`
was superseded by a single-table model.

**Verdict:** drop. This is the only confirmed orphan. AC2 migration in this
ticket targets only this table.

### `trades` — dormant feature, NOT orphan

| Code reference | Path | Status |
| --- | --- | --- |
| Table definition | `petrosa-binance-data-extractor/db/mysql_adapter.py:132` | active |
| Model class | `petrosa-binance-data-extractor/models/trade.py:116` | active |
| Extraction job | `petrosa-binance-data-extractor/jobs/extract_trades.py:102` | active code |
| Pipeline runner | `petrosa-binance-data-extractor/scripts/run_pipeline.py:320` includes `"trades"` in `job_order` | active code |
| Deployed CronJob | `petrosa_k8s/k8s/data-extractor/` | **NOT deployed** (only klines cronjobs present) |

Same pattern as `funding_rates`: full extractor code path exists, no
scheduled CronJob, table sits at 0 rows.

**Verdict:** keep the table. Filing follow-up to resolve product question:
revive or retire the trades-extractor feature.

## AC2 scope

This ticket's AC2 ships a drop migration for `position_contributions` only.
The other 3 tables are NOT touched. AC4 ("zero regression") applies: only
the confirmed-orphan path is closed.

## Follow-ups filed

After this ticket merges:

- **#TBD (extraction_metadata empty-state)** — investigate why klines
  extractor isn't producing extraction_metadata rows despite an active
  writer call path.
- **#TBD (funding_rates dormant)** — operator/product decision: revive the
  funding-rates extractor or retire the feature and drop the table.
- **#TBD (trades dormant)** — operator/product decision: revive the
  trades-extractor or retire the feature and drop the table.

## Refs

- Audit report: [`docs/storage-inventory-report-2026-06-06.md`](./storage-inventory-report-2026-06-06.md)
- Implementation module: `data_manager/maintenance/drop_orphan_position_contributions.py`
- Unit tests: `tests/test_drop_orphan_position_contributions.py`
