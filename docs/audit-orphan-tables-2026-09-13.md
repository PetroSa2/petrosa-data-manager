# AC1 Evidence — 2026-09-02 `petrosa_crypto` Dead-Table Re-audit

**Ticket:** [`PetroSa2/petrosa-data-manager#272`](https://github.com/PetroSa2/petrosa-data-manager/issues/272)
**Follow-up to:** [`PetroSa2/petrosa-data-manager#221`](https://github.com/PetroSa2/petrosa-data-manager/issues/221) / PR #225
**Prior audit:** [`docs/audit-orphan-tables-2026-06-09.md`](./audit-orphan-tables-2026-06-09.md)
**Generated:** 2026-09-13

## Root cause — corrected

The issue as originally filed claimed `MySQLAdapter.connect()` → `_create_tables()` →
`metadata.create_all(self.engine)` (`data_manager/db/mysql_adapter.py:154,299`, **this repo**)
resurrects all six dead tables below. That is **false** for five of the six. Verified against
`data_manager/db/mysql_adapter.py` HEAD (2026-09-13): `_create_tables()` only ever registers
`Table()` objects for `datasets`, `audit_logs`, `health_metrics`, `backfill_jobs`,
`lineage_records`, `schemas`, `daily_pnl` — none of which are dead-table candidates. This
repo's `create_all()` is harmless with respect to the six tables audited here.

The real resurrection mechanisms live in **other repos**:

| Table | Resurrector | Repo |
| --- | --- | --- |
| `strategy_positions` | `CREATE TABLE IF NOT EXISTS` in schema Job | `petrosa_k8s/k8s/tradeengine/strategy-positions-schema-job.yaml:89` |
| `exchange_positions` | same schema Job | `petrosa_k8s/k8s/tradeengine/strategy-positions-schema-job.yaml:158` |
| `position_contributions` | same schema Job | `petrosa_k8s/k8s/tradeengine/strategy-positions-schema-job.yaml:127` |
| `extraction_metadata` | **none found** | — |
| `trades` | **none found** (retired per `petrosa-binance-data-extractor#276`) | — |
| `funding_rates` (MySQL) | `Table()` def + `create_all` on connect | `petrosa-binance-data-extractor/db/mysql_adapter.py:132,154` |

`extraction_metadata` and `trades` have no re-registration source in any currently deployed
image — a plain `DROP TABLE` sticks for these two. The other four will resurrect until the
corresponding cross-repo change lands (AC2/AC4 — see "Cross-repo follow-up" below).

## AC1 — Per-table re-verification (2026-09-13)

| Table | Rows (live, 2026-09-02 audit) | Writer in current deployed images? | Reader? | Verdict |
| --- | --- | --- | --- | --- |
| `strategy_positions` | 0 | none — tradeengine's `strategy_position_manager` keeps positions in-memory | none | **drop** |
| `exchange_positions` | 0 | none | none | **drop** |
| `position_contributions` | 0 | none (re-appeared after #221's drop via the k8s schema Job) | none | **drop** |
| `extraction_metadata` | 0 | none | none | **drop** |
| `trades` | 0 | none — retired per `petrosa-binance-data-extractor#276` | none | **drop** |
| `funding_rates` (MySQL) | 0 | extractor writes here, but real funding data goes to **Mongo** `funding_rates_*` | none (MySQL side) | **drop** |

This re-classifies `funding_rates` and `trades` from the 2026-06-09 audit's "dormant feature,
keep" verdict: `trades` has since been explicitly retired (`#276`), and `funding_rates`' MySQL
path is confirmed vestigial now that funding data has a live Mongo destination. Unlike the
2026-06-09 audit (which found `extraction_metadata` had an active-but-empty writer), this
re-check found **no** live writer for `extraction_metadata` in the currently deployed images —
the writer path referenced in 2026-06-09 is no longer wired into any deployed job.

## AC5 — Retained latent-feature tables (correction from the issue's own text)

`datasets` and `lineage_records` are **not** in scope for this drop — both are defined in this
repo's own `_create_tables()` (`mysql_adapter.py:169,246`) and have no writer, matching the
"latent feature" classification. One correction to the issue body: `lineage_records`'s
"wired reader" (`api/routes/catalog.py` `get_lineage`, around line 259) returns a **hardcoded
`lineage=[]`** — it never actually reads the table. Only `datasets` has a genuine reader
(`catalog_repository.py:46`). This does not change the AC5 call (retain both, they are a
latent/unpopulated catalog feature, not orphan schema) — it only corrects the justification for
`lineage_records`. Retiring the lineage-records API feature entirely is a separate product
decision, out of scope here.

## AC3 — Guarded drop migration

`data_manager/maintenance/drop_orphan_petrosa_crypto_tables_2026_09.py` mirrors the
`drop_orphan_position_contributions.py` (#221) pattern: `--dry-run` / `--apply`, per-table
zero-row guard, idempotent `DROP TABLE IF EXISTS`. Unlike the single-table #221 script, this one
processes all six tables independently in one run (or a `--table` subset) and does **not**
abort the whole batch if one table unexpectedly has rows — it skips that table, drops the rest,
and reports the guard trip so an operator can re-audit before forcing it. See the module
docstring for exit codes.

**No destructive DDL was executed against any live database as part of this ticket.** The
script is delivered as an operator-invoked tool; running it against staging/prod is the AC6
operational step below.

## AC2 / AC4 — Cross-repo follow-up (root-fix, not done in this PR)

This PR is scoped to `petrosa-data-manager` only. The drop only "sticks" permanently once these
land:

1. **`petrosa_k8s`** — remove or neutralize
   `k8s/tradeengine/strategy-positions-schema-job.yaml` so it stops re-provisioning
   `strategy_positions` / `exchange_positions` / `position_contributions`.
2. **`petrosa-binance-data-extractor`** — remove the `funding_rates` `Table()` definition from
   `db/mysql_adapter.py:132` so its own `create_all()` stops re-creating the MySQL-side table.

Per the issue's own AC4 note ("If the coordination proves too broad for one ticket, split AC4
into a `petrosa_k8s` sub-ticket") — recommending exactly that split as the next actionable step;
not filed automatically as part of this run to avoid unrequested cross-repo ticket creation.

`extraction_metadata` and `trades` need no companion change — the drop is final for those two
once applied.

## AC6 — Post-drop verification runbook (operator-executed, not run here)

1. Run `--dry-run` against staging first; confirm all six tables show `row_count: 0` and the
   planned SQL matches this doc.
2. Run `--apply` against staging; confirm the six tables are gone via
   `information_schema.tables`.
3. Restart the data-manager pod in staging; confirm `strategy_positions` /
   `exchange_positions` / `position_contributions` / `funding_rates` reappear (expected, until
   AC2/AC4 land) and `extraction_metadata` / `trades` stay absent.
4. Repeat steps 1–2 against prod once staging is clean.
5. Watch logs/dashboards for 24h for any error referencing the six dropped tables (expected:
   none, since AC1 confirmed zero readers).
6. After the AC2/AC4 cross-repo changes land, re-run `--apply` once more and confirm
   `strategy_positions` / `exchange_positions` / `position_contributions` / `funding_rates` stay
   absent across a pod restart.

## Refs

- Prior audit: [`docs/audit-orphan-tables-2026-06-09.md`](./audit-orphan-tables-2026-06-09.md)
- Implementation module: `data_manager/maintenance/drop_orphan_petrosa_crypto_tables_2026_09.py`
- Unit tests: `tests/test_drop_orphan_petrosa_crypto_tables_2026_09.py`
- Parent epic: `PetroSa2/petrosa_k8s#783`
