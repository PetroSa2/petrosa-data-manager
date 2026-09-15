"""Guarded drop migration for the 2026-09-14 Atlas hygiene audit
(`PetroSa2/petrosa-data-manager#301`).

Mirrors the `--dry-run`/`--apply`, per-target-independent-guard, never-fatal
pattern of `drop_orphan_petrosa_crypto_tables_2026_09.py` (#272/PR #288) and
`drop_stale_market_data_collections_2026_09.py` (#273/PR #289). Unlike those
two modules, the targets here span **multiple Mongo databases** (the whole
point of this ticket), so every target is an explicit ``(db_name,
collection_name)`` pair operated on via ``adapter.client[db_name]`` — the
same cross-database pattern already used read-only by
``MongoDBAdapter.db_stats``/``coll_stats`` (petrosa_k8s#794 storage-inventory
audit) — rather than the single-database ``adapter.db[collection]`` shortcut
the two prior modules use.

## Two corrections to the issue's stated scope (adversarial re-verification
## against repo HEAD + the sibling `petrosa-tradeengine` repo, 2026-09-15)

1. **The `mongodb` database is NOT a stray/dead leftover.** It is the
   intentional (if confusingly-named) write target of
   `petrosa-tradeengine`'s per-pod-boot self-test:
   ``tradeengine/services/data_manager_boot_probe.py:23`` hardcodes
   ``_PROBE_DB = "mongodb"`` — unchanged since the probe's original commit
   (tradeengine#451/#456, 2026-06-05) through #465/#467/#468. Every
   tradeengine pod boot writes one sentinel doc there, reads it back, and a
   24h-TTL best-effort delete removes it (`_cleanup_old_probes_sync`,
   `_TTL_HOURS = 24`). The audit's observed **0 docs** in
   `mongodb.tradeengine_boot_probes` is the *expected steady state* of a
   working self-cleaning probe, not evidence of abandonment. Blindly
   dropping the `mongodb` **database** is therefore out of scope for this
   module — only individual, independently-guarded collection-level
   artifacts inside it are touched, and only when opted in
   (`--include-boot-probe-artifacts`).
2. **The apparent "delete step is leaking documents" bug lives in
   `petrosa-tradeengine`, not this repo.** `data_manager_boot_probe.py`'s
   cleanup is entirely tradeengine-owned client-side code (it calls this
   repo's generic write/query/delete API, but the leak — if the 1,550
   residual `petrosa_data_manager` docs are still current — would be in
   tradeengine's cleanup logic or its invocation, not in anything this repo
   controls). Fixing the root cause is a `petrosa-tradeengine` ticket. This
   module ships an **operator-invoked, TTL-guarded backlog purge** as a
   stopgap data-manager operators can run without waiting on that cross-repo
   fix — it never touches documents inside the TTL window, matching
   tradeengine's own cleanup filter shape exactly
   (``created_at < now - ttl_hours``, ISO-8601 string comparison, same as
   ``_cleanup_old_probes_sync``).

## Targets

Default targets (probe collections with **zero** references in any of the 8
ecosystem repos, confirmed by grep on 2026-09-15 — genuinely dead, not
merely "believed dead"):

* ``petrosa_data_manager._atlas_quota_probe``
* ``petrosa_data_manager._p0_unblock_probe``
* ``petrosa._write_test_probe``

Opt-in targets (``--include-boot-probe-artifacts``), never touched by
default because the collection they belong to has a live, if unusually
named, writer/reader in another repo (see correction #1 above):

* ``mongodb.tradeengine_boot_probes`` — 0-doc guarded drop (safe: Mongo
  auto-recreates a collection on the next insert, so removing an empty one
  causes no data loss and no visible behavior change to the probe).
* ``petrosa.tradeengine_boot_probes`` — same, 0-doc guarded drop.
* ``petrosa_data_manager.tradeengine_boot_probes`` — **not** dropped (it is
  the collection tradeengine's client actually round-trips against when a
  caller omits an explicit db override; some deployments may still target
  it). Instead, a TTL-guarded ``delete_many`` purges only documents older
  than ``--ttl-hours`` (default 24, matching tradeengine's own
  ``_TTL_HOURS``), leaving the collection itself and any recent doc intact.

## Explicitly NOT in scope for this module (see
## `docs/atlas-hygiene-2026-09.md` for the full decision record)

* `binance` DB — retained as the designated leader-election database per
  the issue's own 2026-09-14 correction comment (Option b). No migration,
  no `MONGODB_DB` env change.
* `tickers_*`/`trades_*`/plain `trades` (Mongo) — already covered by the
  shipped `drop_stale_market_data_collections_2026_09.py` (#273/PR #289).
* MySQL 0-row orphans, `datasets`, `lineage_records` — already covered /
  already correctly retained by `drop_orphan_petrosa_crypto_tables_2026_09.py`
  (#272/PR #288); `lineage_records` is a deliberate "latent feature, not
  orphan schema" retain (see `docs/audit-orphan-tables-2026-09-13.md` AC5),
  not reopened here.
* MySQL `klines_*` (all seven: `klines_m1`, `klines_m3`, `klines_h2`,
  `klines_h4`, `klines_h6`, `klines_h8`, `klines_h12`) — **new correction**:
  `GET /api/v1/data/candles?period=<anything>` (`data_manager/api/routes/
  data.py:127,161`) accepts an unrestricted `period` query string and
  `CandleRepository._get_mysql_table_name` (`candle_repository.py:55-62`)
  maps it straight to `klines_{unit}{value}` — the same caller-supplied-
  suffix shape #273 used to gate `trades_{symbol}` behind
  `--include-wired-reader` rather than call it dead. `klines_m1`/`klines_h4`
  additionally back two of `constants.SUPPORTED_TIMEFRAMES` (`1m`, `4h`)
  under the still-live candle-store cutover (#274/#275/PR #287,
  `CANDLE_DATABASE_TYPE` deliberately not flipped in prod). None of the
  seven are added to any drop target anywhere in this PR.

Every drop/purge is idempotent and independently guarded — a guard trip on
one target never blocks the others.

Operator invocation::

    # Dry-run: report doc counts for the three default probe targets only.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_stray_mongo_probes_2026_09 --dry-run

    # Apply: drop the three default probe targets that pass the 0-doc guard.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_stray_mongo_probes_2026_09 --apply

    # Also report/purge the boot-probe artifacts (opt-in; see corrections above):
    ... --apply --include-boot-probe-artifacts --ttl-hours 24

Exit codes:
    0  — success (dry-run guard trips are reported, never fatal)
    2  — MONGODB_URL not set
    3  — --apply mode and at least one target tripped its guard (probe
         collection had docs, or a boot-probe stray copy had docs); targets
         that passed their guard were still processed
    4  — database error
    5  — invocation error
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)

DEFAULT_PROBE_TARGETS: tuple[tuple[str, str], ...] = (
    ("petrosa_data_manager", "_atlas_quota_probe"),
    ("petrosa_data_manager", "_p0_unblock_probe"),
    ("petrosa", "_write_test_probe"),
)

BOOT_PROBE_COLLECTION = "tradeengine_boot_probes"

# Opt-in only — see module docstring correction #1. Never part of the
# default target set.
BOOT_PROBE_STRAY_COPY_TARGETS: tuple[tuple[str, str], ...] = (
    ("mongodb", BOOT_PROBE_COLLECTION),
    ("petrosa", BOOT_PROBE_COLLECTION),
)
BOOT_PROBE_PURGE_DB = "petrosa_data_manager"

DEFAULT_TTL_HOURS = 24


@dataclass
class ProbeDropResult:
    """Per-collection outcome of a 0-doc-guarded probe drop."""

    db_name: str
    collection: str
    dry_run: bool
    existed: bool = False
    doc_count: int = 0
    guard_tripped: bool = False
    dropped: bool = False


@dataclass
class BootProbePurgeResult:
    """Outcome of the TTL-guarded `tradeengine_boot_probes` backlog purge."""

    db_name: str
    collection: str
    dry_run: bool
    ttl_hours: int
    existed: bool = False
    total_doc_count: int = 0
    stale_doc_count: int = 0
    purged_count: int = 0


@dataclass
class MigrationReport:
    probe_results: list[ProbeDropResult] = field(default_factory=list)
    boot_probe_purge: BootProbePurgeResult | None = None
    boot_probe_stray_copies: list[ProbeDropResult] = field(default_factory=list)

    def any_guard_tripped(self) -> bool:
        return any(r.guard_tripped for r in self.probe_results) or any(
            r.guard_tripped for r in self.boot_probe_stray_copies
        )


async def _collection_doc_count(
    adapter: MongoDBAdapter, db_name: str, collection: str
) -> int:
    coll = adapter.client[db_name][collection]
    return await coll.count_documents({})


async def _collection_exists(
    adapter: MongoDBAdapter, db_name: str, collection: str
) -> bool:
    names = await adapter.client[db_name].list_collection_names()
    return collection in names


async def drop_zero_doc_collection(
    adapter: MongoDBAdapter,
    db_name: str,
    collection: str,
    *,
    dry_run: bool,
) -> ProbeDropResult:
    """Guarded drop of a single collection: only proceeds when doc count is
    exactly zero. Never raises — backend errors are reported, not fatal."""
    result = ProbeDropResult(db_name=db_name, collection=collection, dry_run=dry_run)

    try:
        existed = await _collection_exists(adapter, db_name, collection)
    except Exception as e:  # noqa: BLE001 — never abort the batch
        logger.error(
            "%s: %s.%s — existence check failed: %s", __name__, db_name, collection, e
        )
        result.guard_tripped = True
        return result

    result.existed = existed
    if not existed:
        logger.info(
            "%s: %s.%s not present — nothing to do", __name__, db_name, collection
        )
        return result

    try:
        doc_count = await _collection_doc_count(adapter, db_name, collection)
    except Exception as e:  # noqa: BLE001
        logger.error("%s: %s.%s — count failed: %s", __name__, db_name, collection, e)
        result.guard_tripped = True
        return result
    result.doc_count = doc_count

    if doc_count > 0:
        result.guard_tripped = True
        msg = (
            "%s: dry-run sees %d docs in %s.%s — drop would be refused"
            if dry_run
            else "%s: refusing to drop %s.%s: doc-count guard tripped "
            "(docs=%d); this target is no longer empty"
        )
        log_fn = logger.warning if dry_run else logger.error
        if dry_run:
            log_fn(msg, __name__, doc_count, db_name, collection)
        else:
            log_fn(msg, __name__, db_name, collection, doc_count)
        return result

    if dry_run:
        logger.info(
            "%s: dry-run — would drop %s.%s (0 docs)", __name__, db_name, collection
        )
        return result

    await adapter.client[db_name][collection].drop()
    result.dropped = True
    logger.info("%s: dropped %s.%s (0 docs)", __name__, db_name, collection)
    return result


async def purge_stale_boot_probe_docs(
    adapter: MongoDBAdapter,
    *,
    dry_run: bool,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    db_name: str = BOOT_PROBE_PURGE_DB,
    collection: str = BOOT_PROBE_COLLECTION,
    now: datetime | None = None,
) -> BootProbePurgeResult:
    """TTL-guarded backlog purge of `tradeengine_boot_probes` documents.

    Mirrors tradeengine's own `_cleanup_old_probes_sync` filter exactly:
    `created_at < now - ttl_hours` (ISO-8601 string comparison, since
    tradeengine writes `created_at` as `datetime.now(UTC).isoformat()`, a
    string field, not a BSON date). Never deletes anything inside the TTL
    window and never drops the collection — this is a backlog flush, not a
    replacement for tradeengine's own cleanup step.
    """
    now = now if now is not None else datetime.now(UTC)
    result = BootProbePurgeResult(
        db_name=db_name, collection=collection, dry_run=dry_run, ttl_hours=ttl_hours
    )

    try:
        existed = await _collection_exists(adapter, db_name, collection)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "%s: %s.%s — existence check failed: %s", __name__, db_name, collection, e
        )
        return result
    result.existed = existed
    if not existed:
        logger.info(
            "%s: %s.%s not present — nothing to purge", __name__, db_name, collection
        )
        return result

    coll = adapter.client[db_name][collection]
    cutoff = (now - timedelta(hours=ttl_hours)).isoformat()
    stale_filter = {"created_at": {"$lt": cutoff}}

    try:
        result.total_doc_count = await coll.count_documents({})
        result.stale_doc_count = await coll.count_documents(stale_filter)
    except Exception as e:  # noqa: BLE001
        logger.error("%s: %s.%s — count failed: %s", __name__, db_name, collection, e)
        return result

    if result.stale_doc_count == 0:
        logger.info(
            "%s: %s.%s has no docs older than %dh — nothing to purge",
            __name__,
            db_name,
            collection,
            ttl_hours,
        )
        return result

    if dry_run:
        logger.info(
            "%s: dry-run — would purge %d/%d stale docs (older than %dh) from %s.%s",
            __name__,
            result.stale_doc_count,
            result.total_doc_count,
            ttl_hours,
            db_name,
            collection,
        )
        return result

    delete_result = await coll.delete_many(stale_filter)
    result.purged_count = delete_result.deleted_count
    logger.info(
        "%s: purged %d stale docs (older than %dh) from %s.%s",
        __name__,
        result.purged_count,
        ttl_hours,
        db_name,
        collection,
    )
    return result


async def execute_migration(
    adapter: MongoDBAdapter,
    *,
    dry_run: bool,
    include_boot_probe_artifacts: bool = False,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    probe_targets: tuple[tuple[str, str], ...] = DEFAULT_PROBE_TARGETS,
) -> MigrationReport:
    """Run the guarded-drop/purge flow across every target. Never raises —
    per-target errors are reported in the results; the caller decides
    whether that constitutes a failing exit code."""
    report = MigrationReport()

    for db_name, collection in probe_targets:
        report.probe_results.append(
            await drop_zero_doc_collection(
                adapter, db_name, collection, dry_run=dry_run
            )
        )

    if include_boot_probe_artifacts:
        report.boot_probe_purge = await purge_stale_boot_probe_docs(
            adapter, dry_run=dry_run, ttl_hours=ttl_hours
        )
        for db_name, collection in BOOT_PROBE_STRAY_COPY_TARGETS:
            report.boot_probe_stray_copies.append(
                await drop_zero_doc_collection(
                    adapter, db_name, collection, dry_run=dry_run
                )
            )

    return report


def _resolve_ttl_hours(cli_value: int | None) -> int:
    if cli_value is not None:
        if cli_value < 0:
            logger.warning("Clamping --ttl-hours=%d to minimum 0", cli_value)
            return 0
        return cli_value
    raw = os.getenv("BOOT_PROBE_PURGE_TTL_HOURS")
    if raw is None:
        return DEFAULT_TTL_HOURS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer BOOT_PROBE_PURGE_TTL_HOURS=%r; using default %d",
            raw,
            DEFAULT_TTL_HOURS,
        )
        return DEFAULT_TTL_HOURS
    if value < 0:
        logger.warning("Clamping BOOT_PROBE_PURGE_TTL_HOURS=%d to minimum 0", value)
        return 0
    return value


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.drop_stray_mongo_probes_2026_09",
        description=(
            "Guarded drop of confirmed-dead Atlas probe collections + "
            "opt-in TTL-guarded tradeengine_boot_probes backlog purge. "
            "See data-manager#301 for the AC1 evidence and "
            "docs/atlas-hygiene-2026-09.md for the full decision record."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show doc counts per target without modifying anything.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Drop/purge every target that passes its guard.",
    )
    parser.add_argument(
        "--include-boot-probe-artifacts",
        action="store_true",
        help=(
            "Also process the tradeengine_boot_probes artifacts: TTL-guarded "
            "backlog purge in petrosa_data_manager, and 0-doc-guarded drop "
            "of the stray copies in mongodb/petrosa. Off by default — the "
            "collection has a live cross-repo writer/reader (see module "
            "docstring correction #1)."
        ),
    )
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=None,
        help=(
            "Override BOOT_PROBE_PURGE_TTL_HOURS for this run "
            f"(default {DEFAULT_TTL_HOURS}, matches tradeengine's own "
            "cleanup TTL)."
        ),
    )
    return parser


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    connection_string = os.getenv("MONGODB_URL")
    if not connection_string:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    ttl_hours = _resolve_ttl_hours(args.ttl_hours)

    adapter = MongoDBAdapter(connection_string=connection_string)
    adapter.connect()
    try:
        report = await execute_migration(
            adapter,
            dry_run=args.dry_run,
            include_boot_probe_artifacts=args.include_boot_probe_artifacts,
            ttl_hours=ttl_hours,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a database error
        logger.error("database error during migration: %s", exc)
        return 4
    finally:
        adapter.disconnect()

    dropped = [f"{r.db_name}.{r.collection}" for r in report.probe_results if r.dropped]
    guarded = [
        f"{r.db_name}.{r.collection}" for r in report.probe_results if r.guard_tripped
    ]
    logger.info("migration summary: dropped=%s guarded(skipped)=%s", dropped, guarded)

    if report.boot_probe_purge is not None:
        p = report.boot_probe_purge
        logger.info(
            "boot-probe purge summary: db=%s collection=%s stale=%d purged=%d",
            p.db_name,
            p.collection,
            p.stale_doc_count,
            p.purged_count,
        )
        stray_dropped = [
            f"{r.db_name}.{r.collection}"
            for r in report.boot_probe_stray_copies
            if r.dropped
        ]
        stray_guarded = [
            f"{r.db_name}.{r.collection}"
            for r in report.boot_probe_stray_copies
            if r.guard_tripped
        ]
        logger.info(
            "boot-probe stray-copy summary: dropped=%s guarded(skipped)=%s",
            stray_dropped,
            stray_guarded,
        )

    if not args.dry_run and report.any_guard_tripped():
        return 3

    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
