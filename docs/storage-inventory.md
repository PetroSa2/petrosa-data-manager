# Storage Inventory Audit

A **strictly read-only** audit of every MongoDB database + collection and every
MySQL schema + table reachable from the cluster. Diagnostic only — it deletes
nothing. Closes the loop on [`petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783)
(the P0 Atlas-storage incident) by attributing the residual >400 MB across
backends.

Tracking issue: [`petrosa_k8s#794`](https://github.com/PetroSa2/petrosa_k8s/issues/794)
(impl_repo `petrosa-data-manager`).

## When to run

- After a major Mongo cleanup, to root-cause whatever footprint is left.
- When `mongo_atlas_storage_used_bytes` (the alert from
  [`petrosa_k8s#786`](https://github.com/PetroSa2/petrosa_k8s/issues/786))
  crosses the 80% warning threshold.
- Before scoping a retention/migration ticket — so the ticket starts from
  measurement, not assumption.

It is **not** a recurring CronJob. The companion to this audit is the
klines-retention CronJob ([`petrosa_k8s#787`](https://github.com/PetroSa2/petrosa_k8s/issues/787)),
which ships paused; the audit is what tells the operator whether to unpause
it and what other retention work to file.

## Safety model (two layers — both required)

| Layer | Mechanism | Code reference |
| --- | --- | --- |
| 1. Credential | `SELECT`-only MySQL user + Mongo role limited to `read` + `listDatabases` / `clusterMonitor` | provisioned by the operator |
| 2. Code | Audit calls only `listDatabases` / `dbStats` / `collStats` / `list_collections` / `find_one` (Mongo) and `information_schema` SELECTs (MySQL); bypasses `MySQLAdapter.connect()` (which runs DDL) | `data_manager.maintenance.storage_inventory` + `data_manager.db.mysql_adapter.create_read_only_engine` |

Layer 1 is the **real** safety guarantee. Layer 2 is the second belt — it
proves the data-manager codebase itself does not emit DDL on the audit path,
but cannot defend against an over-privileged credential. The unit-test
suite asserts no write/delete/insert/update/drop method is called on the
mock adapters during a full audit pass.

## Required DB privileges

### MongoDB Atlas

The audit needs to enumerate every database, including `local`/`admin`/`config`,
and run `dbStats` + `collStats`. The cleanest Atlas built-in role is one of:

- `clusterMonitor` (project-level) — grants `listDatabases`, `dbStats`,
  `collStats`, `replSetGetStatus`, etc. without read access to user data.
- `readAnyDatabase` — broader (allows reading documents); only needed if
  the audit's best-effort `newest_doc_age` field lookups are also required.

If both roles are unavailable, the audit will exit with code `13`
(privilege denied) — request the missing privilege rather than working
around it.

### MySQL

`SELECT` on `information_schema` is sufficient. No DDL is ever issued.

## Required environment

The audit reads only the standard data-manager env vars:

- `MONGODB_URL` — full connection string (the var data-manager actually
  reads; **not** `MONGODB_URI`).
- `MYSQL_URI` — full SQLAlchemy connection string (e.g.
  `mysql+pymysql://user:pass@host:3306/petrosa`).
- `LOG_LEVEL` (optional; default `INFO`).

When running in-cluster, these come from the `petrosa-sensitive-credentials`
secret. Inject **both** explicitly in the Job manifest — the existing
`klines-retention-cronjob.yaml` only injects `MONGODB_URL`, so a naïve copy
would yield a Mongo-only audit. The `MYSQL_URI` key lives in the same
secret (see `petrosa_k8s/k8s/shared/database-init/klines-view-mapper.yaml`).

## Invocation

```bash
# Full audit (both backends)
opentelemetry-instrument python -m data_manager.maintenance.storage_inventory --json

# Mongo only (e.g. if the MySQL credential is still being provisioned)
opentelemetry-instrument python -m data_manager.maintenance.storage_inventory --mongo-only --json

# MySQL only
opentelemetry-instrument python -m data_manager.maintenance.storage_inventory --mysql-only --json
```

Stdout carries a markdown report. With `--json`, a fenced JSON blob is
appended so the report can be machine-processed.

### Exit codes (`petrosa_k8s#794` AC7)

| Code | Meaning |
| --- | --- |
| `0` | Both backends OK (or the selected subset is OK) |
| `10` | Mongo backend failed |
| `11` | MySQL backend failed |
| `12` | Both backends failed |
| `13` | Privilege/permission denied on a required admin command |

## In-cluster execution

Local/laptop execution is **expected to fail** on IP allow-listing — Atlas
and the MySQL backend are not reachable from arbitrary workstations. The
canonical path is a one-shot `kind: Job` in the `petrosa-apps` namespace:

1. `kubectl apply -f petrosa_k8s/k8s/data-extractor/storage-inventory-job.yaml`
   (added separately under [`petrosa_k8s#794`](https://github.com/PetroSa2/petrosa_k8s/issues/794)'s
   sibling PR — the manifest is not in this PR because the
   `k8s/data-extractor/` directory is GitOps-auto-applied and the Job
   placement needs explicit operator review per
   [`project_petrosa_k8s_gitops_auto_apply.md`](../../../petrosa_k8s/docs/)).
2. Wait for completion: `kubectl wait --for=condition=complete job/storage-inventory -n petrosa-apps --timeout=20m`.
3. Capture logs **before `ttlSecondsAfterFinished` reaps the pod**:
   `kubectl logs job/storage-inventory -n petrosa-apps > storage-inventory-$(date -u +%FT%H-%M-%SZ).log`.
4. Commit the redacted findings to `docs/storage-inventory-report-YYYY-MM-DD.md`.

Run it during a **low-trading window** — `collStats` across hundreds of
live collections adds latency to a shared Atlas cluster serving live
trading.

## Findings report template

Use this template for the committed report. Names + sizes only; redact any
connection strings or credentials that may have leaked into logs.

```markdown
# Storage Inventory Findings — YYYY-MM-DD

**Run window:** YYYY-MM-DDTHH:MM:SSZ → YYYY-MM-DDTHH:MM:SSZ
**Run-as credential:** `<readonly-username>` (project `<atlas-project-id>`)
**Cluster (Mongo):** `<atlas-cluster-id>` (URI redacted)
**Cluster (MySQL):** `<mysql-host>:3306` (URI redacted)
**Exit code:** `<0|10|11|12|13>`

## 1. Where the residual ~XXX MB lives (AC3)

Paste the "400 MB Attribution" table from the Job stdout. State the
dominant consumer and confirm/refute each KEY HYPOTHESIS from the
tech-spec:

- [ ] `candles_*` left behind by the manual cleanup?
- [ ] Time-series bucket storage (`system.buckets.klines_*`) dominates the
      klines logical size?
- [ ] Per-symbol/per-timeframe collection explosion?
- [ ] Atlas-only footprint (oplog + system DBs) the app never sees?
- [ ] Candle duplication across Mongo + MySQL backends?
- [ ] Multiple Mongo DBs (e.g. `petrosa` + `petrosa_data_manager` + `test_tradeengine`)?
- [ ] `signals` duplicated across Mongo + MySQL?

## 2. Inventory by backend (AC1)

### MongoDB

Paste the "MongoDB databases" section from the Job stdout.

### MySQL

Paste the "MySQL schemas" section from the Job stdout.

## 3. Duplication + orphan analysis (AC4 + AC5)

- Duplicated candle timeframes: `[...]`
- Orphan-suspect Mongo collections: `[...]`
- Orphan-suspect MySQL tables: `[...]`

## 4. Follow-up remediation tickets (AC8)

Filed via `petrosa_k8s/scripts/create-project-item.sh` with the `cross-repo`
label and linked to `#783`:

- `petrosa_k8s#XXX` — <retention/consolidation/orphan-drop ticket title>
- ...
```

## Limitations + caveats

- **Point-in-time snapshot.** Re-run after any large retention/migration
  pass to refresh.
- **`TABLE_ROWS` is an InnoDB estimate.** Storage verdict comes from
  `DATA_LENGTH` + `INDEX_LENGTH`, not the row count.
- **`storageSize` vs `dataSize`.** The Atlas quota counts on-disk
  `storageSize` (compressed). The audit reports both; the attribution
  table sums on-disk `storageSize`. Logical `dataSize` is informational.
- **Heuristic classification.** `orphan-suspect` and `duplicated` are
  flags for human review, never auto-action. Remediation is always a
  follow-up reviewed ticket, not part of this work item.
- **DB names embedded in URIs are resolved at runtime** by the Job, not
  knowable from the manifests beforehand. Static literals in this repo
  (`petrosa`, `petrosa_data_manager`, `test_tradeengine`) are seeds for
  classification; the live audit is what reveals every effective DB.

## Related

- [`petrosa_k8s#783`](https://github.com/PetroSa2/petrosa_k8s/issues/783)
  — P0 Atlas-storage incident (parent epic).
- [`petrosa_k8s#786`](https://github.com/PetroSa2/petrosa_k8s/issues/786)
  — Atlas storage alerts (the warning that should re-trigger this audit).
- [`petrosa_k8s#787`](https://github.com/PetroSa2/petrosa_k8s/issues/787)
  — klines-retention CronJob (ships paused; awaits this audit's verdict).
- [`docs/klines-retention.md`](klines-retention.md) — sibling maintenance
  job; same packaging pattern (`python -m`, env-driven, OTel-wrapped).
- `_bmad-output/implementation-artifacts/tech-spec-storage-inventory-audit-mongo-mysql.md`
  in the `petrosa_k8s` repo — full design rationale + adversarial-review
  findings F1–F12 that hardened the spec.
