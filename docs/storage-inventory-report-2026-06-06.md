# Storage Inventory Audit Report — 2026-06-06

**Generated:** `2026-06-06T18:15:48.575808Z`
**Job:** `storage-inventory` in namespace `petrosa-apps`
**Manifest provenance:** PetroSa2/petrosa_k8s#799 (Task 6 of #794)
**Audit ticket:** PetroSa2/petrosa_k8s#800 (Tasks 7-9 of #794)
**Parent epic:** PetroSa2/petrosa_k8s#783 (P0 Atlas-storage incident)

---

## Backend Status

| Backend | Status | Error |
| --- | --- | --- |
| MongoDB | **FAILED** | `'AsyncIOMotorCommandCursor' object is not iterable` in `listDatabases` |
| MySQL | **OK** | — |

> **Impact:** All Mongo hypotheses are unresolved. A bug fix + re-run is required for the Mongo half.
> See remediation ticket filed under Finding F-BUG-1 below.

---

## AC2 — Investigation Questions

### Q1: Which DBs/collections/tables are live vs orphan-suspect?

**MongoDB:** UNKNOWN — backend failed; re-run required after bug fix (F-BUG-1).

**MySQL `petrosa_crypto`** (26 base tables, 14 views):

| Table | rows~ | last updated | classification |
| --- | ---: | --- | --- |
| `audit_logs` | 4,533 | active | **live** |
| `backfill_jobs` | 3,034 | active | **live** |
| `datasets` | 0 | — | live (schema-only, expected) |
| `exchange_positions` | 0 | — | live (schema-only, expected) |
| `extraction_metadata` | 0 | — | **orphan-suspect** |
| `funding_rates` | 0 | — | **orphan-suspect** |
| `health_metrics` | 393,218 | 2026-06-06 | live |
| `klines_d1` | 13,604 | active | live |
| `klines_h1` | 331,388 | active | live |
| `klines_h12` | 0 | — | live (schema-only) |
| `klines_h2` | 0 | — | live (schema-only) |
| `klines_h4` | 0 | — | live (schema-only) |
| `klines_h6` | 0 | — | live (schema-only) |
| `klines_h8` | 0 | — | live (schema-only) |
| `klines_m1` | 0 | — | live (schema-only) |
| `klines_m15` | 1,466,256 | active | live |
| `klines_m30` | 767,121 | active | live |
| `klines_m3` | 0 | — | live (schema-only) |
| `klines_m5` | 4,688,089 | active | live |
| `lineage_records` | 0 | — | live (schema-only, expected) |
| `position_contributions` | 0 | — | **orphan-suspect** |
| `positions` | 0 | — | live (schema-only, expected) |
| `schemas` | 0 | — | live (schema-only) |
| `signals` | 44,507 | 2026-06-06 | live |
| `strategy_positions` | 0 | — | live (schema-only, expected) |
| `trades` | 0 | — | **orphan-suspect** |
| *14 views* | — | — | view (0 bytes, excluded from storage) |

**Confirmed orphan-suspects:** `extraction_metadata`, `funding_rates`, `position_contributions`, `trades` — all 0 rows, no recent update_time, no known active writer in current k8s manifests.

---

### Q2: Where do the >400 MB live?

> The "400 MB mystery" framing is significantly understated. **MySQL klines alone hold 2.71 GB.**

#### Attribution (on-disk bytes)

| Category | Bytes | Human |
| --- | ---: | --- |
| `mongo_candles_bytes` | 0 | FAILED |
| `mongo_klines_logical_bytes` | 0 | FAILED |
| `mongo_klines_buckets_bytes` | 0 | FAILED |
| `mongo_other_app_bytes` | 0 | FAILED |
| `mongo_oplog_bytes` | 0 | FAILED |
| `mongo_system_dbs_bytes` | 0 | FAILED |
| **`mysql_klines_bytes`** | **2,840,903,680** | **2.71 GB** |
| `mysql_other_bytes` | 171,982,848 | 164 MB |
| **MySQL total** | **3,012,886,528** | **2.87 GB** |

#### MySQL top consumers (data + index bytes)

| Table | DATA_LENGTH | INDEX_LENGTH | Total | rows~ |
| --- | ---: | ---: | ---: | ---: |
| `klines_m5` | 1,029,701,632 | 785,383,424 | **1,815,085,056** | 4,688,089 |
| `klines_m15` | 327,155,712 | 250,937,344 | **578,093,056** | 1,466,256 |
| `klines_m30` | 177,209,344 | 126,812,160 | **304,021,504** | 767,121 |
| `health_metrics` | 75,251,712 | 68,452,352 | **143,704,064** | 393,218 |
| `klines_h1` | 79,478,784 | 55,312,384 | **134,791,168** | 331,388 |
| `klines_d1` | 3,686,400 | 4,767,744 | **8,454,144** | 13,604 |
| `signals` | 17,317,888 | 5,816,320 | **23,134,208** | 44,507 |

#### KEY HYPOTHESIS verdict (MySQL confirmed; Mongo UNRESOLVED)

| # | Hypothesis | Verdict | Evidence |
| --- | --- | --- | --- |
| H1 | `candles_*` left behind in Mongo after `klines_*` cleanup | **UNRESOLVED** | Mongo backend FAILED |
| H2 | `klines_*` time-series `system.buckets` residual in Mongo | **UNRESOLVED** | Mongo backend FAILED |
| H3 | Per-symbol collection explosion overhead in Mongo | **UNRESOLVED** | Mongo backend FAILED |
| H4 | Index bloat in Mongo | **UNRESOLVED** | Mongo backend FAILED |
| H5 | Candle duplication across Mongo + MySQL | **PARTIAL** | MySQL klines are live and active; Mongo side unknown — `duplicated_candle_timeframes: []` (empty, likely because Mongo failed) |
| H6 | Other unknown Mongo collections | **UNRESOLVED** | Mongo backend FAILED |
| H7 | `signals` duplicated (Mongo + MySQL) | **CONFIRMED for MySQL** | MySQL `signals` has 44,507 rows; Mongo side unknown |

**Confirmed headline finding:** MySQL `klines_m5` alone is **1.81 GB** on-disk (data + index). The retention CronJob (`petrosa_k8s#787`) ships with `spec.suspend: true` — nothing is auto-pruning. This is the dominant and confirmed storage consumer.

---

### Q3: Is candle data duplicated across Mongo + MySQL?

**Partial answer only.** MySQL has live klines data across multiple timeframes (m5, m15, m30, h1, d1 are the populated ones). Whether Mongo also holds `candles_*` or `klines_*` for the same timeframes is unknown until the Mongo bug is fixed and the audit re-run.

The `CANDLE_DATABASE_TYPE` env var controls which backend the `CandleRepository` writes to. The k8s manifests for the current production environment set `DB_ADAPTER: mysql` and `EXTRACTOR_DB_ADAPTER: mysql`, which suggests Mongo candle writes are inactive in production — but this must be verified by the Mongo audit re-run (H1).

---

### Q4: Is MySQL sound?

**Partially sound, two concerns:**

1. **`klines_m5` unbounded growth** — 4.7M rows, 1.81 GB. The retention CronJob is paused. Without enabling it, this table will continue to grow at the backfill/live ingestion rate.
2. **`health_metrics` unbounded growth** — 393K rows, 144 MB. No retention mechanism identified. This is a non-kline table that the klines-retention CronJob does NOT touch. A separate retention/TTL strategy is needed.
3. **Views vs base tables** — 14 views (`klines_12h`, `klines_15m`, etc.) are correctly zero-byte SQL views over the base `klines_*` tables. No soundness issue.
4. **Zero-row orphan-suspect tables** — `extraction_metadata`, `funding_rates`, `position_contributions`, `trades` — 0 rows, no evident writer. Safe candidates for review + drop (not done in this ticket — zero deletion per AC4).

---

## Confirmed Findings & Remediation Tickets

| ID | Finding | Severity | Action |
| --- | --- | --- | --- |
| F-BUG-1 | `storage_inventory.py` Mongo `listDatabases` uses `AsyncIOMotorCommandCursor` synchronously | High | Fix async iteration bug + re-run audit |
| F-RETAIN-1 | `klines_m5` (1.81 GB, 4.7M rows) — retention CronJob paused, unbounded growth | High | Enable/verify klines-retention CronJob for m5 |
| F-RETAIN-2 | `health_metrics` (144 MB, 393K rows) — no TTL/retention mechanism | Medium | Define and implement retention policy |
| F-ORPHAN-1 | `extraction_metadata`, `funding_rates`, `position_contributions`, `trades` — 0 rows, no known writer | Low | Review + drop confirmed-orphan tables (separate ticket, zero deletion here) |
| F-MONGO-RERUN | Mongo audit incomplete — all Mongo hypotheses unresolved | High | Re-run after F-BUG-1 fix; file Mongo-specific findings from that run |

---

## Raw Job Output (JSON)

Redacted: names and sizes only. No connection strings or credentials.

```json
{
  "generated_at": "2026-06-06T18:15:48.575808Z",
  "mongo_ok": false,
  "mysql_ok": true,
  "mongo_error": "'AsyncIOMotorCommandCursor' object is not iterable",
  "mysql_error": null,
  "mongo_databases": [],
  "mongo_oplog": null,
  "mysql_schemas": [
    {
      "name": "petrosa_crypto",
      "tables": [
        {"schema": "petrosa_crypto", "name": "audit_logs", "table_type": "BASE TABLE", "data_length": 1589248, "index_length": 1900544, "table_rows_estimated": 4533, "create_time": "2025-10-21T07:40:57", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "backfill_jobs", "table_type": "BASE TABLE", "data_length": 458752, "index_length": 524288, "table_rows_estimated": 3034, "create_time": "2025-10-23T13:10:14", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "datasets", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 16384, "table_rows_estimated": 0, "create_time": "2026-03-04T22:55:24", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "exchange_positions", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 32768, "table_rows_estimated": 0, "create_time": "2026-03-04T22:55:24", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "extraction_metadata", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 16384, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "orphan-suspect"},
        {"schema": "petrosa_crypto", "name": "funding_rates", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "orphan-suspect"},
        {"schema": "petrosa_crypto", "name": "health_metrics", "table_type": "BASE TABLE", "data_length": 75251712, "index_length": 68452352, "table_rows_estimated": 393218, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_d1", "table_type": "BASE TABLE", "data_length": 3686400, "index_length": 4767744, "table_rows_estimated": 13604, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_h1", "table_type": "BASE TABLE", "data_length": 79478784, "index_length": 55312384, "table_rows_estimated": 331388, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_h12", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_h2", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_h4", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_h6", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_h8", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_m1", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_m15", "table_type": "BASE TABLE", "data_length": 327155712, "index_length": 250937344, "table_rows_estimated": 1466256, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_m30", "table_type": "BASE TABLE", "data_length": 177209344, "index_length": 126812160, "table_rows_estimated": 767121, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_m3", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "klines_m5", "table_type": "BASE TABLE", "data_length": 1029701632, "index_length": 785383424, "table_rows_estimated": 4688089, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "lineage_records", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 16384, "table_rows_estimated": 0, "create_time": "2026-05-09T10:51:37", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "position_contributions", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 81920, "table_rows_estimated": 0, "create_time": "2026-03-04T22:55:24", "update_time": null, "classification": "orphan-suspect"},
        {"schema": "petrosa_crypto", "name": "positions", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 98304, "table_rows_estimated": 0, "create_time": "2026-03-04T22:55:24", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "schemas", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-23T13:10:14", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "signals", "table_type": "BASE TABLE", "data_length": 17317888, "index_length": 5816320, "table_rows_estimated": 44507, "create_time": "2025-10-15T00:20:38", "update_time": "2026-06-06T15:11:44", "classification": "live"},
        {"schema": "petrosa_crypto", "name": "strategy_positions", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 98304, "table_rows_estimated": 0, "create_time": "2026-03-04T22:55:24", "update_time": null, "classification": "live"},
        {"schema": "petrosa_crypto", "name": "trades", "table_type": "BASE TABLE", "data_length": 16384, "index_length": 49152, "table_rows_estimated": 0, "create_time": "2025-10-15T00:20:38", "update_time": null, "classification": "orphan-suspect"}
      ],
      "error": null
    }
  ],
  "attribution": {
    "mongo_candles_bytes": 0,
    "mongo_klines_logical_bytes": 0,
    "mongo_klines_buckets_bytes": 0,
    "mongo_other_app_bytes": 0,
    "mongo_oplog_bytes": 0,
    "mongo_system_dbs_bytes": 0,
    "mysql_klines_bytes": 2840903680,
    "mysql_other_bytes": 171982848
  },
  "duplicated_candle_timeframes": []
}
```

---

## AC4 Compliance

**Zero data deleted or modified.** This report is read-only. All remediation is filed as separate follow-up tickets.
