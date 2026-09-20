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
    MONGODB_SIGNALS_TTL_SECONDS  signals collection TTL window in seconds
                                 (default 3600 = 1 hour; companion to
                                 petrosa-bot-ta-analysis#267 AC6; MySQL
                                 `petrosa_crypto.signals` is the durable
                                 historic store, see generic.py dual-write)
    MONGODB_ALERTS_TTL_SECONDS   alerts collection TTL window in seconds
                                 (default 604800 = 7 days; data-manager#271 AC6)
    MONGODB_CONFIG_RATE_LIMITS_TTL_SECONDS
                                 config_rate_limits TTL window in seconds
                                 (default 3600 = 1 hour; data-manager#302,
                                 matches petrosa_k8s/scripts/mongodb/
                                 init-rate-limiting.js's documented window)
    MONGODB_CONFIG_RATE_LIMITS_DATABASE
                                 database housing config_rate_limits (default
                                 "petrosa" — the shared cross-service DB, NOT
                                 this repo's own database; data-manager#302)

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

# --------------------------------------------------------------------------- #
# signals TTL — companion to petrosa-bot-ta-analysis#267 AC6
# --------------------------------------------------------------------------- #
# The `signals` collection (trading signals persisted via the generic
# `/api/v1/mongodb/signals` insert endpoint) has NO confirmed reader today
# (see #267's required follow-up ticket: "who reads signals, and when?").
# Shipping any new high-frequency Mongo write onto the shared Atlas M0
# (512 MB) cluster — which has already suffered four P0 quota outages — is
# only acceptable with a mandatory, bounded retention mechanism in the same
# change. Unlike `intents.received_at`, callers already send their own
# `timestamp` as an ISO *string* (JSON-compat requirement in
# ta_bot/models/signal.py `to_dict()`), so a TTL on `timestamp` would never
# actually expire anything — the generic insert route
# (data_manager/api/routes/generic.py `insert_records`) therefore stamps a
# dedicated, unconditional, real BSON Date field `_ttl_inserted_at` on every
# `signals` document specifically for this TTL index to key on.
SIGNALS_COLLECTION = "signals"
SIGNALS_TTL_FIELD = "_ttl_inserted_at"
SIGNALS_TTL_INDEX_NAME = "_ttl_inserted_at_ttl"
DEFAULT_SIGNALS_TTL_SECONDS = 3600  # 1 hour — tightened 2026-09-20. The prior
# 7-day default let the collection grow silently (204 -> 132k+ docs in 4
# days) because the TTL index itself had never actually been applied in
# production (see docs/readerless-collections-audit-2026-09-15.md), pushing
# the shared Atlas M0 to 83% of its 512 MB quota. MySQL `petrosa_crypto.signals`
# is now the durable historic store (dual-written by generic.py's
# `insert_records`, see `_build_mysql_signal_record`), so Mongo only needs to
# hold a short recent-activity window. Operator-overridable via
# MONGODB_SIGNALS_TTL_SECONDS.

# data-manager#271 — `alerts` TTL companion. The `alerts` collection
# (alert_dispatcher._persist) is a write-only audit trail with NO confirmed
# reader (no api/routes read it), so it MUST NOT accumulate unbounded on the
# shared Atlas M0 (512 MB) cluster that has suffered four quota P0s. The
# publisher `timestamp` is serialized to an ISO *string* by
# model_dump(mode="json"), so the dispatcher stamps a dedicated, unconditional,
# real BSON `Date` field `_ttl_inserted_at` on every document — mirroring the
# `signals` stamp — and the TTL index here keys on that field.
ALERTS_COLLECTION = "alerts"
ALERTS_TTL_FIELD = "_ttl_inserted_at"
ALERTS_TTL_INDEX_NAME = "_ttl_inserted_at_ttl"
DEFAULT_ALERTS_TTL_SECONDS = 604800  # 7 days — data-manager#271 AC3

# --------------------------------------------------------------------------- #
# config_rate_limits TTL — data-manager#302
# --------------------------------------------------------------------------- #
# Corrected finding (data-manager#302, live Atlas re-query 2026-09-15): unlike
# `alerts`/`signals`, this collection is NOT readerless — it is written AND
# read by `petrosa_otel.ConfigRateLimiter.check_rate_limit` (sliding-window
# quota check, `rate_limiter.py` `coll.find(query)`), wired into tradeengine
# (`tradeengine/api.py`), petrosa-bot-ta-analysis (`ta_bot/main.py`), this repo
# (`data_manager/main.py`), and petrosa-realtime-strategies
# (`strategies/main.py`). It lives in the **shared** `petrosa` Atlas database
# (`petrosa_k8s/k8s/shared/configmaps/petrosa-common-config.yaml`
# `MONGODB_DATABASE: "petrosa"`) — a different database than this repo's own
# `petrosa_data_manager` (which holds `intents`/`signals`/`alerts`).
#
# `petrosa_k8s/scripts/mongodb/init-rate-limiting.js` already defines a
# 1-hour TTL index on `timestamp`, but a live read-only query confirmed it was
# **never actually applied**: the oldest live document dates to 2026-03-13
# (six months of unbounded accumulation at the time of this audit) with only
# the default `_id_` index present. That — not "no consumer" — is the real
# root cause of the "growing" symptom the ticket described.
#
# This repo is one of four writers/readers of the shared collection; the
# idempotent ensure-function below covers the instance THIS service's own
# `ConfigRateLimiter` wiring touches (`data_manager/main.py`). Wiring the same
# self-heal into the other three repos, and/or actually running
# `init-rate-limiting.js` against the live `petrosa` database, is a cross-repo
# follow-up flagged for the operator (see docs/readerless-collections-audit-
# 2026-09-15.md) — not resolved by this change alone.
CONFIG_RATE_LIMITS_COLLECTION = "config_rate_limits"
CONFIG_RATE_LIMITS_TTL_FIELD = "timestamp"
CONFIG_RATE_LIMITS_TTL_INDEX_NAME = "timestamp_ttl_1h"
DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS = 3600  # 1 hour — matches
# petrosa_k8s/scripts/mongodb/init-rate-limiting.js's documented window.
DEFAULT_CONFIG_RATE_LIMITS_DATABASE = "petrosa"  # shared cross-service DB

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

# Per-collection overrides to the default decision above. A sibling graduates
# here once volume evidence warrants active retention. The `trades` override was
# removed with the trades retention job (data-manager#254): the `trades`
# collection is being retired entirely (binance-data-extractor#276), so it now
# falls back to the default sibling decision until the collection is dropped.
SIBLING_DECISION_OVERRIDES: dict[str, str] = {
    # data-manager#271 AC1 — decision documented as REQUIRED by the ticket:
    # `alerts` has no confirmed reader (grep across api/routes found zero), so it
    # is treated as EPHEMERAL and graduated to a managed TTL here. The dedicated
    # `_ttl_inserted_at` BSON-Date TTL index (7-day default,
    # MONGODB_ALERTS_TTL_SECONDS) caps storage; the kill-switch
    # (PETROSA_ALERT_PERSIST_ENABLED) in the dispatcher stops the write entirely.
    # A separate follow-up ticket must define the intended reader (alerts
    # dashboard/API vs dispatch-only) before this decision is revisited.
    "alerts": (
        "ephemeral — no confirmed reader (data-manager#271); managed by the "
        "dedicated `_ttl_inserted_at` TTL index, window "
        "MONGODB_ALERTS_TTL_SECONDS (default 7 days)"
    ),
}


def _sibling_decision(collection: str) -> str:
    """Return the retention decision for a sibling collection."""
    return SIBLING_DECISION_OVERRIDES.get(collection, SIBLING_RETENTION_DECISION)


@dataclass
class TtlIndexConfig:
    """Resolved configuration for one TTL-index maintenance run."""

    database: str = DEFAULT_DATABASE
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    signals_ttl_seconds: int = DEFAULT_SIGNALS_TTL_SECONDS
    alerts_ttl_seconds: int = DEFAULT_ALERTS_TTL_SECONDS
    config_rate_limits_database: str = DEFAULT_CONFIG_RATE_LIMITS_DATABASE
    config_rate_limits_ttl_seconds: int = DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS
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
    signals_ttl_seconds = _parse_int_env(
        env,
        "MONGODB_SIGNALS_TTL_SECONDS",
        DEFAULT_SIGNALS_TTL_SECONDS,
        minimum=60,
    )
    alerts_ttl_seconds = _parse_int_env(
        env,
        "MONGODB_ALERTS_TTL_SECONDS",
        DEFAULT_ALERTS_TTL_SECONDS,
        minimum=60,
    )
    config_rate_limits_ttl_seconds = _parse_int_env(
        env,
        "MONGODB_CONFIG_RATE_LIMITS_TTL_SECONDS",
        DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS,
        minimum=60,
    )
    config_rate_limits_database = (
        env.get("MONGODB_CONFIG_RATE_LIMITS_DATABASE")
        or DEFAULT_CONFIG_RATE_LIMITS_DATABASE
    )
    return TtlIndexConfig(
        database=resolve_database_name(env),
        ttl_seconds=ttl_seconds,
        signals_ttl_seconds=signals_ttl_seconds,
        alerts_ttl_seconds=alerts_ttl_seconds,
        config_rate_limits_database=config_rate_limits_database,
        config_rate_limits_ttl_seconds=config_rate_limits_ttl_seconds,
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


async def _repair_ttl_window(
    db,
    coll,
    collection_name: str,
    index_name: str,
    field: str,
    ttl_seconds: int,
) -> str:
    """Change an existing TTL index's ``expireAfterSeconds`` in place.

    Tries ``collMod`` first (MongoDB's documented in-place TTL repair — no
    index rebuild). Live-confirmed 2026-09-20: the Atlas DB user this job
    runs as is NOT granted ``collMod`` on this cluster (``AtlasError code
    8000, "user is not allowed to do action [collMod]"``), even though it
    already holds ``dropIndex``/``createIndex`` (used elsewhere in this same
    file for the "wrong field" repair path). Falls back to drop+recreate on
    any :class:`PyMongoError`, so a TTL-window change (e.g. shrinking
    `signals` from 7 days to 1 hour) still actually lands instead of
    silently no-op'ing on a permission error. Returns the action taken:
    ``"collmod"`` or ``"recreated"``.
    """
    try:
        await db.command(
            "collMod",
            collection_name,
            index={"name": index_name, "expireAfterSeconds": ttl_seconds},
        )
        return "collmod"
    except PyMongoError as exc:
        logger.warning(
            "collMod denied on %s index=%s (%s) — falling back to drop+recreate",
            collection_name,
            index_name,
            exc,
        )
        await coll.drop_index(index_name)
        await coll.create_index(
            [(field, ASCENDING)],
            name=index_name,
            expireAfterSeconds=ttl_seconds,
        )
        return "recreated"


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
        elif dry_run:
            action = "collmod"
        else:
            action = await _repair_ttl_window(
                db, coll, INTENTS_COLLECTION, TTL_INDEX_NAME, TTL_FIELD, ttl_seconds
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


async def ensure_signals_ttl_index(
    db,
    db_name: str,
    *,
    ttl_seconds: int = DEFAULT_SIGNALS_TTL_SECONDS,
    dry_run: bool = False,
) -> TtlIndexResult:
    """Idempotently ensure the TTL index on `signals._ttl_inserted_at`.

    Companion to petrosa-bot-ta-analysis#267 AC6. Mirrors
    :func:`ensure_intents_ttl_index`'s idempotent create/collmod/noop logic,
    minus the legacy-index-drop step (no prior broken TTL index exists on
    this collection — it never had one).

    * No-ops if the index already matches the desired spec.
    * Repairs the TTL window via ``collMod`` if the field is right but
      ``expireAfterSeconds`` differs.
    * Creates the index if absent.
    * If ``signals`` does not exist yet (no writer has run), still ensures
      the index will be present at collection-creation time by creating it
      directly — MongoDB permits creating an index on a not-yet-existing
      collection, which implicitly creates the collection empty.
    """
    coll = db[SIGNALS_COLLECTION]
    info = await coll.index_information()

    existing = info.get(SIGNALS_TTL_INDEX_NAME)
    if existing is not None and _index_key_fields(existing) == [SIGNALS_TTL_FIELD]:
        current_ttl = existing.get("expireAfterSeconds")
        if current_ttl == ttl_seconds:
            action = "noop"
        elif dry_run:
            action = "collmod"
        else:
            action = await _repair_ttl_window(
                db,
                coll,
                SIGNALS_COLLECTION,
                SIGNALS_TTL_INDEX_NAME,
                SIGNALS_TTL_FIELD,
                ttl_seconds,
            )
    elif existing is not None:
        # Name squatting on the wrong field — drop and recreate cleanly.
        action = "recreated"
        if not dry_run:
            await coll.drop_index(SIGNALS_TTL_INDEX_NAME)
            await coll.create_index(
                [(SIGNALS_TTL_FIELD, ASCENDING)],
                name=SIGNALS_TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )
    else:
        action = "created"
        if not dry_run:
            await coll.create_index(
                [(SIGNALS_TTL_FIELD, ASCENDING)],
                name=SIGNALS_TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )

    result = TtlIndexResult(
        database=db_name,
        collection=SIGNALS_COLLECTION,
        index_name=SIGNALS_TTL_INDEX_NAME,
        field=SIGNALS_TTL_FIELD,
        ttl_seconds=ttl_seconds,
        action=action,
        dropped_legacy=[],
        dry_run=dry_run,
    )

    logger.info(
        "signals_ttl_index: db=%s collection=%s index=%s key={%s: 1} "
        "expireAfterSeconds=%d action=%s%s",
        result.database,
        result.collection,
        result.index_name,
        result.field,
        result.ttl_seconds,
        result.action,
        " (dry-run)" if dry_run else "",
    )
    return result


async def ensure_alerts_ttl_index(
    db,
    db_name: str,
    *,
    ttl_seconds: int = DEFAULT_ALERTS_TTL_SECONDS,
    dry_run: bool = False,
) -> TtlIndexResult:
    """Idempotently ensure the TTL index on `alerts._ttl_inserted_at`.

    data-manager#271 — the `alerts` collection is a write-only audit trail
    with no confirmed reader; it must NOT accumulate unbounded on the shared
    Atlas M0 (512 MB). Mirrors :func:`ensure_signals_ttl_index`'s idempotent
    create/collmod/noop logic (no legacy broken index to clear — this
    collection never had a TTL).

    * No-ops if the index already matches the desired spec.
    * Repairs the TTL window via ``collMod`` if the field is right but
      ``expireAfterSeconds`` differs.
    * Creates the index if absent.
    * If ``alerts`` does not exist yet, still ensures the index will be
      present at collection-creation time by creating it directly — MongoDB
      permits creating an index on a not-yet-existing collection.
    """
    coll = db[ALERTS_COLLECTION]
    info = await coll.index_information()

    existing = info.get(ALERTS_TTL_INDEX_NAME)
    if existing is not None and _index_key_fields(existing) == [ALERTS_TTL_FIELD]:
        current_ttl = existing.get("expireAfterSeconds")
        if current_ttl == ttl_seconds:
            action = "noop"
        elif dry_run:
            action = "collmod"
        else:
            action = await _repair_ttl_window(
                db,
                coll,
                ALERTS_COLLECTION,
                ALERTS_TTL_INDEX_NAME,
                ALERTS_TTL_FIELD,
                ttl_seconds,
            )
    elif existing is not None:
        # Name squatting on the wrong field — drop and recreate cleanly.
        action = "recreated"
        if not dry_run:
            await coll.drop_index(ALERTS_TTL_INDEX_NAME)
            await coll.create_index(
                [(ALERTS_TTL_FIELD, ASCENDING)],
                name=ALERTS_TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )
    else:
        action = "created"
        if not dry_run:
            await coll.create_index(
                [(ALERTS_TTL_FIELD, ASCENDING)],
                name=ALERTS_TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )

    result = TtlIndexResult(
        database=db_name,
        collection=ALERTS_COLLECTION,
        index_name=ALERTS_TTL_INDEX_NAME,
        field=ALERTS_TTL_FIELD,
        ttl_seconds=ttl_seconds,
        action=action,
        dropped_legacy=[],
        dry_run=dry_run,
    )

    logger.info(
        "alerts_ttl_index: db=%s collection=%s index=%s key={%s: 1} "
        "expireAfterSeconds=%d action=%s%s",
        result.database,
        result.collection,
        result.index_name,
        result.field,
        result.ttl_seconds,
        result.action,
        " (dry-run)" if dry_run else "",
    )
    return result


async def ensure_config_rate_limits_ttl_index(
    db,
    db_name: str,
    *,
    ttl_seconds: int = DEFAULT_CONFIG_RATE_LIMITS_TTL_SECONDS,
    dry_run: bool = False,
) -> TtlIndexResult:
    """Idempotently ensure the TTL index on `config_rate_limits.timestamp`.

    data-manager#302 — corrected finding: this collection has a confirmed
    reader (`petrosa_otel.ConfigRateLimiter.check_rate_limit`) and a
    documented 1-hour TTL design (`petrosa_k8s/scripts/mongodb/
    init-rate-limiting.js`), but live re-query showed the TTL index was never
    actually applied (oldest live document: 2026-03-13). Unlike `signals`/
    `alerts`, no dedicated stamp field is needed here — `ConfigRateLimiter`
    already writes a native BSON ``Date`` in `timestamp`
    (`petrosa_otel/rate_limiter.py`), so the index keys on that field
    directly, matching the js migration's own design.

    Mirrors :func:`ensure_alerts_ttl_index`'s idempotent create/collmod/noop
    logic. Callers targeting the shared `petrosa` database MUST pass a `db`
    handle selected from that database (see :data:`DEFAULT_CONFIG_RATE_LIMITS_DATABASE`)
    — NOT this repo's own `petrosa_data_manager` database.
    """
    coll = db[CONFIG_RATE_LIMITS_COLLECTION]
    info = await coll.index_information()

    existing = info.get(CONFIG_RATE_LIMITS_TTL_INDEX_NAME)
    if existing is not None and _index_key_fields(existing) == [
        CONFIG_RATE_LIMITS_TTL_FIELD
    ]:
        current_ttl = existing.get("expireAfterSeconds")
        if current_ttl == ttl_seconds:
            action = "noop"
        elif dry_run:
            action = "collmod"
        else:
            action = await _repair_ttl_window(
                db,
                coll,
                CONFIG_RATE_LIMITS_COLLECTION,
                CONFIG_RATE_LIMITS_TTL_INDEX_NAME,
                CONFIG_RATE_LIMITS_TTL_FIELD,
                ttl_seconds,
            )
    elif existing is not None:
        # Name squatting on the wrong field — drop and recreate cleanly.
        action = "recreated"
        if not dry_run:
            await coll.drop_index(CONFIG_RATE_LIMITS_TTL_INDEX_NAME)
            await coll.create_index(
                [(CONFIG_RATE_LIMITS_TTL_FIELD, ASCENDING)],
                name=CONFIG_RATE_LIMITS_TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )
    else:
        action = "created"
        if not dry_run:
            await coll.create_index(
                [(CONFIG_RATE_LIMITS_TTL_FIELD, ASCENDING)],
                name=CONFIG_RATE_LIMITS_TTL_INDEX_NAME,
                expireAfterSeconds=ttl_seconds,
            )

    result = TtlIndexResult(
        database=db_name,
        collection=CONFIG_RATE_LIMITS_COLLECTION,
        index_name=CONFIG_RATE_LIMITS_TTL_INDEX_NAME,
        field=CONFIG_RATE_LIMITS_TTL_FIELD,
        ttl_seconds=ttl_seconds,
        action=action,
        dropped_legacy=[],
        dry_run=dry_run,
    )

    logger.info(
        "config_rate_limits_ttl_index: db=%s collection=%s index=%s key={%s: 1} "
        "expireAfterSeconds=%d action=%s%s",
        result.database,
        result.collection,
        result.index_name,
        result.field,
        result.ttl_seconds,
        result.action,
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
        decision = _sibling_decision(name)
        if name not in present_collections:
            logger.info(
                "intents_ttl_index[audit]: db=%s collection=%s present=False "
                "decision=%s",
                db_name,
                name,
                decision,
            )
            results.append(
                SiblingAuditResult(
                    collection=name,
                    present=False,
                    ttl_indexes={},
                    decision=decision,
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
            decision,
        )
        results.append(
            SiblingAuditResult(
                collection=name,
                present=True,
                ttl_indexes=ttl_indexes,
                decision=decision,
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
    parser.add_argument(
        "--signals-ttl-seconds",
        type=int,
        default=None,
        help="Override MONGODB_SIGNALS_TTL_SECONDS for this run.",
    )
    parser.add_argument(
        "--alerts-ttl-seconds",
        type=int,
        default=None,
        help="Override MONGODB_ALERTS_TTL_SECONDS for this run.",
    )
    parser.add_argument(
        "--config-rate-limits-ttl-seconds",
        type=int,
        default=None,
        help="Override MONGODB_CONFIG_RATE_LIMITS_TTL_SECONDS for this run.",
    )
    parser.add_argument(
        "--config-rate-limits-database",
        type=str,
        default=None,
        help=(
            "Override the database targeted for config_rate_limits (default: "
            f"MONGODB_CONFIG_RATE_LIMITS_DATABASE env var, else "
            f"'{DEFAULT_CONFIG_RATE_LIMITS_DATABASE}' — the shared cross-service "
            "DB, NOT this repo's own database)."
        ),
    )
    parser.add_argument(
        "--skip-config-rate-limits",
        action="store_true",
        help=(
            "Skip the config_rate_limits TTL check entirely (data-manager#302 "
            "targets a different, shared database than intents/signals/alerts; "
            "use this to keep a run scoped to this repo's own database only)."
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

    config = load_config_from_env()
    config.dry_run = bool(args.dry_run)
    if args.ttl_seconds is not None:
        config.ttl_seconds = max(60, args.ttl_seconds)
    if args.signals_ttl_seconds is not None:
        config.signals_ttl_seconds = max(60, args.signals_ttl_seconds)
    if args.alerts_ttl_seconds is not None:
        config.alerts_ttl_seconds = max(60, args.alerts_ttl_seconds)
    if args.config_rate_limits_ttl_seconds is not None:
        config.config_rate_limits_ttl_seconds = max(
            60, args.config_rate_limits_ttl_seconds
        )
    if args.config_rate_limits_database:
        config.config_rate_limits_database = args.config_rate_limits_database

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
        # Companion to petrosa-bot-ta-analysis#267 AC6 — mandatory TTL for
        # any new high-frequency Mongo write; see module docstring above.
        await ensure_signals_ttl_index(
            db,
            config.database,
            ttl_seconds=config.signals_ttl_seconds,
            dry_run=config.dry_run,
        )
        # data-manager#271 AC6 (BLOCKING) — the `alerts` write-only audit
        # trail must NOT remain a TTL-less Mongo writer on the shared Atlas M0.
        await ensure_alerts_ttl_index(
            db,
            config.database,
            ttl_seconds=config.alerts_ttl_seconds,
            dry_run=config.dry_run,
        )
        await audit_sibling_collections(db, config.database)
        # data-manager#302 — config_rate_limits lives in a DIFFERENT (shared,
        # cross-service) database than intents/signals/alerts above; select
        # it explicitly rather than reusing `db`. Same Atlas cluster/client,
        # different logical database.
        if not args.skip_config_rate_limits:
            rate_limits_db = adapter.client[config.config_rate_limits_database]
            await ensure_config_rate_limits_ttl_index(
                rate_limits_db,
                config.config_rate_limits_database,
                ttl_seconds=config.config_rate_limits_ttl_seconds,
                dry_run=config.dry_run,
            )
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
