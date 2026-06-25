"""Idempotent TTL-index maintenance job for the ``intents`` collection.

Targets `PetroSa2/petrosa-data-manager#244` — the root-cause fix for three
MongoDB Atlas M0 quota P0 incidents (2026-06-10, 2026-06-16, 2026-06-19).

Why the previous attempts failed
--------------------------------
``petrosa_k8s#820`` shipped a TTL index on the ``intents`` collection keyed on
``timestamp`` (the ticket body says ``createdAt``; the manifest actually used
``timestamp`` — both are wrong for different reasons). That index reused the
name ``timestamp_1``, which the application itself creates as a *plain* index on
every consumer startup (``MongoDBAdapter.ensure_indexes``). The two definitions
collide (``IndexOptionsConflict``), so the TTL was silently lost on the next
deploy and documents accumulated until the 512 MB Atlas quota halted all writes.

The fix
-------
Maintain a TTL index under a **dedicated name** (``received_at_ttl_1d``) on the
**subscriber-set** ``received_at`` field:

* ``received_at`` is always written by data-manager as a real BSON ``Date``
  (``IntentEvent.received_at`` ``default_factory=datetime.now(UTC)``), so the TTL
  monitor actually purges — unlike a publisher-supplied ``timestamp`` that could
  arrive as a string, and unlike the non-existent ``createdAt``.
* The dedicated name never collides with the app-managed ``timestamp_1`` index.

This job is the operator-runnable, auditable companion to the app-startup
self-heal added in ``MongoDBAdapter.ensure_indexes``. It is **idempotent**
(AC2), logs the database + collection + index spec at INFO on every run (AC3),
runs against an **explicit** database name (AC4), and audits sibling collections
for the same class of defect (AC5).

Invocation::

    # Dry-run — report what would change, mutate nothing.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.intents_ttl_index --dry-run

    # Apply — create/repair the TTL index and drop legacy broken ones.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.intents_ttl_index --apply

Environment contract::

    MONGODB_URL                  (required) full connection string
    MONGODB_DATABASE             explicit DB name; falls back to MONGODB_DB,
                                 then to "petrosa_data_manager" (AC4)
    MONGODB_INTENTS_TTL_SECONDS  TTL window in seconds (default 86400 = 1 day)

Exit codes::

    0  success (apply completed, or dry-run completed cleanly)
    2  MONGODB_URL not set
    4  MongoDB error
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field

try:
    from pymongo import ASCENDING
    from pymongo.errors import PyMongoError
except ImportError:  # pragma: no cover - pymongo is a runtime dependency
    ASCENDING = 1  # type: ignore[assignment]

    class PyMongoError(Exception):  # type: ignore[no-redef]
        """Fallback when pymongo is unavailable (keeps import-time safe)."""


from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)

INTENTS_COLLECTION = "intents"
TTL_FIELD = "received_at"
TTL_INDEX_NAME = "received_at_ttl_1d"
LEGACY_BROKEN_FIELD = "createdAt"
DEFAULT_DATABASE = "petrosa_data_manager"
DEFAULT_TTL_SECONDS = 86400  # 1 day

# AC5 — sibling collections that could share the same unbounded-growth defect.
# Documented decision: these are audit/financial-event trails. They are NOT
# auto-expired by this job pending per-collection volume evidence; instead they
# are audited (their current index state is logged) on every run, and the Atlas
# data-size leading-indicator alert (AC6, in petrosa_k8s) is the safety net that
# tells us if any of them starts to threaten the quota. Flip a collection to a
# managed TTL here (with its documented timestamp field) once evidence warrants.
SIBLING_COLLECTIONS: tuple[str, ...] = (
    "alerts",
    "cio_decisions",
    "execution_events",
    "pnl_events",
    "trades",
)
SIBLING_RETENTION_DECISION = (
    "retain — audit/financial trail; not auto-expired pending volume evidence; "
    "covered by the Atlas data-size leading-indicator alert (data-manager#244 AC6)"
)


@dataclass
class TtlIndexConfig:
    """Resolved configuration for one TTL-index maintenance run."""

    database: str = DEFAULT_DATABASE
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    dry_run: bool = False


@dataclass
class TtlIndexResult:
    """Outcome of ensuring the intents TTL index."""

    database: str
    collection: str
    index_name: str
    field: str
    ttl_seconds: int
    action: str  # one of: noop | created | collmod | recreated
    dropped_legacy: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class SiblingAuditResult:
    """Per-sibling-collection audit outcome (AC5)."""

    collection: str
    present: bool
    ttl_indexes: dict[str, int]  # index name -> expireAfterSeconds
    decision: str


def resolve_database_name(environ: dict[str, str] | None = None) -> str:
    """Resolve the EXPLICIT target database name (AC4).

    Precedence: ``MONGODB_DATABASE`` (the name suggested by the ticket) →
    ``MONGODB_DB`` (the existing repo convention) → ``petrosa_data_manager``.
    Never derived from the connection-string path, because that is exactly how
    k8s#820 ended up targeting the wrong database.
    """
    env = environ if environ is not None else os.environ
    return env.get("MONGODB_DATABASE") or env.get("MONGODB_DB") or DEFAULT_DATABASE


def load_config_from_env(environ: dict[str, str] | None = None) -> TtlIndexConfig:
    """Build a :class:`TtlIndexConfig` from environment variables."""
    env = environ if environ is not None else os.environ
    ttl_seconds = _parse_int_env(
        env, "MONGODB_INTENTS_TTL_SECONDS", DEFAULT_TTL_SECONDS, minimum=60
    )
    return TtlIndexConfig(
        database=resolve_database_name(env),
        ttl_seconds=ttl_seconds,
        dry_run=False,
    )


def _parse_int_env(env: dict[str, str], key: str, default: int, *, minimum: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer %s=%r; using default %d", key, raw, default
        )
        return default
    if value < minimum:
        logger.warning("Clamping %s=%d to minimum %d", key, value, minimum)
        return minimum
    return value


def _index_key_fields(meta: dict) -> list[str]:
    """Return the ordered key field names for an index_information() entry."""
    return [field_name for field_name, _direction in meta.get("key", [])]


def _is_legacy_createdat_ttl(meta: dict) -> bool:
    """True if this index is a TTL index keyed solely on the broken ``createdAt``."""
    return "expireAfterSeconds" in meta and _index_key_fields(meta) == [
        LEGACY_BROKEN_FIELD
    ]


async def ensure_intents_ttl_index(
    db,
    db_name: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    dry_run: bool = False,
) -> TtlIndexResult:
    """Idempotently ensure the canonical ``received_at`` TTL index on ``intents``.

    * Drops any legacy ``createdAt``-keyed TTL index (AC2).
    * No-ops if ``received_at_ttl_1d`` already matches the desired spec (AC2).
    * Repairs the TTL window via ``collMod`` if the index exists with the right
      field but a different ``expireAfterSeconds`` (AC2).
    * Creates the index if absent (AC1).
    * Logs database + collection + index spec at INFO on every run (AC3).
    """
    coll = db[INTENTS_COLLECTION]
    info = await coll.index_information()

    # AC2 — drop legacy createdAt-based TTL indexes.
    dropped_legacy: list[str] = []
    for name, meta in info.items():
        if _is_legacy_createdat_ttl(meta):
            dropped_legacy.append(name)
            if not dry_run:
                await coll.drop_index(name)

    existing = info.get(TTL_INDEX_NAME)
    if existing is not None and _index_key_fields(existing) == [TTL_FIELD]:
        current_ttl = existing.get("expireAfterSeconds")
        if current_ttl == ttl_seconds:
            action = "noop"
        else:
            action = "collmod"
            if not dry_run:
                await db.command(
                    "collMod",
                    INTENTS_COLLECTION,
                    index={"name": TTL_INDEX_NAME, "expireAfterSeconds": ttl_seconds},
                )
    elif existing is not None:
        # Name squatting on the wrong field — drop and recreate cleanly.
        action = "recreated"
        if not dry_run:
            await coll.drop_index(TTL_INDEX_NAME)
            await coll.create_index(
                [(TTL_FIELD, ASCENDING)],
                name=TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )
    else:
        action = "created"
        if not dry_run:
            await coll.create_index(
                [(TTL_FIELD, ASCENDING)],
                name=TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )

    result = TtlIndexResult(
        database=db_name,
        collection=INTENTS_COLLECTION,
        index_name=TTL_INDEX_NAME,
        field=TTL_FIELD,
        ttl_seconds=ttl_seconds,
        action=action,
        dropped_legacy=dropped_legacy,
        dry_run=dry_run,
    )

    # AC3 — auditable single-line record of exactly what this run touched.
    logger.info(
        "intents_ttl_index: db=%s collection=%s index=%s key={%s: 1} "
        "expireAfterSeconds=%d action=%s dropped_legacy=%s%s",
        result.database,
        result.collection,
        result.index_name,
        result.field,
        result.ttl_seconds,
        result.action,
        result.dropped_legacy or "[]",
        " (dry-run)" if dry_run else "",
    )
    return result


async def audit_sibling_collections(
    db,
    db_name: str,
) -> list[SiblingAuditResult]:
    """Audit sibling collections for the same unbounded-growth defect (AC5).

    Read-only: logs each sibling's current TTL-index state and the documented
    retention decision so the audit trail is grep-able and the per-collection
    decision is explicit.
    """
    present_collections = set(await db.list_collection_names())
    results: list[SiblingAuditResult] = []

    for name in SIBLING_COLLECTIONS:
        if name not in present_collections:
            logger.info(
                "intents_ttl_index[audit]: db=%s collection=%s present=False "
                "decision=%s",
                db_name,
                name,
                SIBLING_RETENTION_DECISION,
            )
            results.append(
                SiblingAuditResult(
                    collection=name,
                    present=False,
                    ttl_indexes={},
                    decision=SIBLING_RETENTION_DECISION,
                )
            )
            continue

        info = await db[name].index_information()
        ttl_indexes = {
            idx_name: meta["expireAfterSeconds"]
            for idx_name, meta in info.items()
            if "expireAfterSeconds" in meta
        }
        logger.info(
            "intents_ttl_index[audit]: db=%s collection=%s present=True "
            "ttl_indexes=%s decision=%s",
            db_name,
            name,
            ttl_indexes or "{}",
            SIBLING_RETENTION_DECISION,
        )
        results.append(
            SiblingAuditResult(
                collection=name,
                present=True,
                ttl_indexes=ttl_indexes,
                decision=SIBLING_RETENTION_DECISION,
            )
        )

    return results


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.intents_ttl_index",
        description=(
            "Idempotently ensure the received_at TTL index on the intents "
            "collection and audit sibling collections (data-manager#244)."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned index changes without mutating the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Create/repair the TTL index and drop legacy broken indexes.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="Override MONGODB_INTENTS_TTL_SECONDS for this run.",
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

    config = load_config_from_env()
    config.dry_run = bool(args.dry_run)
    if args.ttl_seconds is not None:
        config.ttl_seconds = max(60, args.ttl_seconds)

    connection_string = os.getenv("MONGODB_URL")
    if not connection_string:
        logger.error("MONGODB_URL is not set; cannot connect to MongoDB")
        return 2

    adapter = MongoDBAdapter(connection_string=connection_string)
    adapter.connect()
    try:
        # AC4 — select the database by EXPLICIT name, never the adapter's
        # connection-string-derived default.
        db = adapter.client[config.database]
        await ensure_intents_ttl_index(
            db,
            config.database,
            ttl_seconds=config.ttl_seconds,
            dry_run=config.dry_run,
        )
        await audit_sibling_collections(db, config.database)
    except PyMongoError as exc:
        logger.error("MongoDB error during intents TTL maintenance: %s", exc)
        return 4
    finally:
        adapter.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
