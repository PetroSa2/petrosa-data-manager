"""
Constants and configuration for the Petrosa Data Manager service.
"""

import logging
import os

# Service information
SERVICE_NAME = "petrosa-data-manager"
SERVICE_VERSION = "1.0.0"

# Environment
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# NATS Configuration
NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
NATS_CONSUMER_SUBJECT = os.getenv(
    "NATS_CONSUMER_SUBJECT", "binance.futures.websocket.data"
)
# Audit-trail subscribers (cross-service identifier contract, P0.2 epic)
NATS_INTENT_SUBJECT = os.getenv("NATS_INTENT_SUBJECT", "cio.intent.>")
NATS_DECISION_SUBJECT = os.getenv("NATS_DECISION_SUBJECT", "signals.trading.>")
# P0.2c: tradeengine publishes onto <prefix>.<strategy_id>; we subscribe
# with the `>` wildcard. Prefix comes from petrosa-common-config.
NATS_TOPIC_EXECUTION_EVENTS = os.getenv(
    "NATS_TOPIC_EXECUTION_EVENTS", "execution.events"
)
NATS_EXECUTION_EVENTS_SUBJECT = os.getenv(
    "NATS_EXECUTION_EVENTS_SUBJECT", f"{NATS_TOPIC_EXECUTION_EVENTS}.>"
)
# P0.2d: data-manager itself publishes onto <prefix>.<strategy_id> after the
# P4.1 P&L computation lands; the subscriber side ships first so the
# collection + indexes + subscription exist when the publisher comes online.
NATS_TOPIC_PNL_EVENTS = os.getenv("NATS_TOPIC_PNL_EVENTS", "pnl.events")
NATS_PNL_EVENTS_SUBJECT = os.getenv(
    "NATS_PNL_EVENTS_SUBJECT", f"{NATS_TOPIC_PNL_EVENTS}.>"
)
NATS_CLIENT_NAME = f"{SERVICE_NAME}-consumer"
NATS_CONNECT_TIMEOUT = int(os.getenv("NATS_CONNECT_TIMEOUT", "10"))
NATS_MAX_RECONNECT_ATTEMPTS = int(os.getenv("NATS_MAX_RECONNECT_ATTEMPTS", "10"))
NATS_RECONNECT_TIME_WAIT = int(os.getenv("NATS_RECONNECT_TIME_WAIT", "2"))

# MySQL Configuration
MYSQL_HOST = os.getenv("MYSQL_HOST", os.getenv("POSTGRES_HOST", "localhost"))
MYSQL_PORT = int(os.getenv("MYSQL_PORT", os.getenv("POSTGRES_PORT", "3306")))
MYSQL_USER = os.getenv("MYSQL_USER", os.getenv("POSTGRES_USER", "root"))
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", os.getenv("POSTGRES_PASSWORD", ""))
MYSQL_DB = os.getenv("MYSQL_DB", os.getenv("POSTGRES_DB", "petrosa_data_manager"))
MYSQL_URI = os.getenv(
    "MYSQL_URI",
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}",
)

MONGODB_HOST = os.getenv("MONGODB_HOST", "localhost")
MONGODB_PORT = int(os.getenv("MONGODB_PORT", "27017"))
MONGODB_USER = os.getenv("MONGODB_USER", "")
MONGODB_PASSWORD = os.getenv("MONGODB_PASSWORD", "")
MONGODB_DB = os.getenv("MONGODB_DB", "petrosa_data_manager")
CANDLE_MONGO_DATABASE = os.getenv("CANDLE_MONGO_DATABASE", "petrosa_data_manager")
MONGODB_URL = os.getenv(
    "MONGODB_URL",
    f"mongodb://{MONGODB_USER}:{MONGODB_PASSWORD}@{MONGODB_HOST}:{MONGODB_PORT}/{MONGODB_DB}"
    if MONGODB_USER
    else f"mongodb://{MONGODB_HOST}:{MONGODB_PORT}/{MONGODB_DB}",
)

# Intents TTL retention (data-manager#244). The `intents` audit-trail collection
# grows ~280k docs/day; without a working TTL it exhausts the Atlas M0 512 MB quota
# and halts all cluster writes (three P0s: 2026-06-10/06-16/06-19). A TTL index on
# the subscriber-set `received_at` datetime (NOT the publisher-supplied `timestamp`,
# and NOT the non-existent `createdAt` that k8s#820 mistakenly targeted) caps it.
# Default 1 day → ~280 MB steady state (~45% headroom). Read by BOTH the app-startup
# self-heal (MongoDBAdapter.ensure_indexes) and the standalone maintenance job
# (data_manager.maintenance.intents_ttl_index) so they never disagree.
INTENTS_TTL_SECONDS = int(os.getenv("MONGODB_INTENTS_TTL_SECONDS", "86400"))

# Alerts TTL retention (data-manager#271). The `alerts` audit-trail collection is
# a write-only sink (alert_dispatcher._persist) with NO confirmed reader; on the
# shared Atlas M0 512 MB cluster it grows unbounded against a hard cap that has
# already produced four quota P0s (#783/#819/#881/#899). The publisher `timestamp`
# is serialized to an ISO *string* before persist (model_dump mode="json"), so a
# TTL index on it would never expire anything; the dispatcher stamps a dedicated
# real BSON `Date` field `_ttl_inserted_at` on every row (same pattern as the
# `signals` stamp in #267) and this window config drives the `_ttl_inserted_at_ttl`
# index. Default 7 days comfortably exceeds any operational alert-review need
# while capping storage at the retention window. Read by BOTH the app-startup
# self-heal (MongoDBAdapter.ensure_indexes) and the standalone maintenance job
# (data_manager.maintenance.intents_ttl_index) so they never disagree.
ALERTS_TTL_SECONDS = int(os.getenv("MONGODB_ALERTS_TTL_SECONDS", "604800"))

# CIO decisions TTL retention (2026-09-20). Unlike `signals`/`alerts`,
# `cio_decisions` has confirmed live readers: `audit_evaluator.py`'s staleness
# detector (30 min lookback), `api/routes/lifecycle.py`'s intents/decisions/
# execution_events join, and `portfolio/state_service.py`'s point-in-time
# reconstruction. The binding constraint is the join against `intents`, which
# already expires at 1 day (INTENTS_TTL_SECONDS) — a lifecycle lookup can't
# succeed past that anyway, so 1 day is the minimal window that keeps every
# current read working (the staleness detector needs far less; state_at's
# `recent_decisions` chain degrades gracefully, not incorrectly, for `at`
# older than the window — see `PortfolioStateService.state_at`). MySQL
# `cio_decisions` (see `mysql_adapter.py`, dual-written by
# `decision_consumer.py`) is the unbounded permanent copy. Read by BOTH the
# app-startup self-heal (MongoDBAdapter.ensure_indexes) and the standalone
# maintenance job (data_manager.maintenance.intents_ttl_index) so they never
# disagree — the same discipline that was missing for `signals` and let it
# grow silently for 4 days.
CIO_DECISIONS_TTL_SECONDS = int(os.getenv("MONGODB_CIO_DECISIONS_TTL_SECONDS", "86400"))

# Trades retention (data-manager#246). The `trades` collection (raw public-trade
# ticks written directly by the binance-futures extractor) grows unbounded at
# ~22 MB/day and drove the 4th Atlas M0 quota P0 (2026-07-01: 870k docs / ~349 MB,
# writes blocked). Its `timestamp`/`trade_time`/`extracted_at` fields are stored as
# ISO-8601 STRINGS, not BSON Dates, so a native TTL index is impossible (same root
# pattern as #244). Instead a scheduled job deletes docs older than this window via
# lexicographic string comparison on the ISO-8601 `timestamp` field. Default 7 days;
# revisit toward 3 days if raw ticks still trend toward the quota.
TRADES_RETENTION_DAYS = int(os.getenv("TRADES_RETENTION_DAYS", "7"))

# Feature Flags
ENABLE_AUDITOR = os.getenv("ENABLE_AUDITOR", "true").lower() == "true"
ENABLE_BACKFILLER = os.getenv("ENABLE_BACKFILLER", "true").lower() == "true"
ENABLE_ANALYTICS = os.getenv("ENABLE_ANALYTICS", "true").lower() == "true"
ENABLE_API = os.getenv("ENABLE_API", "true").lower() == "true"
ENABLE_INTENT_CONSUMER = os.getenv("ENABLE_INTENT_CONSUMER", "true").lower() == "true"
ENABLE_DECISION_CONSUMER = (
    os.getenv("ENABLE_DECISION_CONSUMER", "true").lower() == "true"
)
ENABLE_EXECUTION_EVENTS_CONSUMER = (
    os.getenv("ENABLE_EXECUTION_EVENTS_CONSUMER", "true").lower() == "true"
)
ENABLE_PNL_CONSUMER = os.getenv("ENABLE_PNL_CONSUMER", "true").lower() == "true"
# Alert spine subscriber (FR66 / #183). Subscribes to `alerts.>`, persists
# every event into the `alerts` Mongo collection, attempts delivery to the
# operator webhook (or marks delivered_mock when no webhook URL is set),
# and enforces per-category rate limiting + summary rollup. Defaults true
# so a fresh deploy is ready to receive alerts the moment producers light
# up (e.g. petrosa-tradeengine reconciliation mismatch on AC2.e).
ENABLE_ALERT_DISPATCHER = os.getenv("ENABLE_ALERT_DISPATCHER", "true").lower() == "true"
NATS_ALERTS_SUBJECT = os.getenv("NATS_ALERTS_SUBJECT", "alerts.>")
# P4.1 follow-up (#652): publisher side that binds the
# `ExecutionEventsConsumer.on_persisted` hook to a NATS publisher emitting
# `pnl.events.<strategy_id>`. Defaults true so a fresh deploy lights up the
# subject. Set to "false" only when the broker is unavailable (CI w/o NATS)
# or when temporarily quiescing the publisher during a migration.
ENABLE_PNL_PUBLISHER = os.getenv("ENABLE_PNL_PUBLISHER", "true").lower() == "true"
# Lookback window for the cold-start replay that seeds the long-lived
# PnlCalculator from historical `execution_events`. Operator can shorten
# this for faster restarts on a backfilled environment.
PNL_PUBLISHER_SEED_DAYS = int(os.getenv("PNL_PUBLISHER_SEED_DAYS", "30"))
# P2.4 execution evaluator (#595)
ENABLE_EXECUTION_EVALUATOR = (
    os.getenv("ENABLE_EXECUTION_EVALUATOR", "true").lower() == "true"
)
# P2.4 evaluator tick cadence. Default ≈ half the error-rate window so a
# committed-unhealthy verdict surfaces within roughly one detection-time
# budget after the underlying anomaly begins.
EXECUTION_EVALUATOR_TICK_INTERVAL = int(
    os.getenv("EXECUTION_EVALUATOR_TICK_INTERVAL", "150")
)
# P2.5 audit evaluator (#596)
ENABLE_AUDIT_EVALUATOR = os.getenv("ENABLE_AUDIT_EVALUATOR", "true").lower() == "true"
# P2.5 audit evaluator tick cadence (seconds). Default 5 min so each tick's
# consume/persist delta covers a meaningful slice without thrashing.
AUDIT_EVALUATOR_TICK_INTERVAL = int(os.getenv("AUDIT_EVALUATOR_TICK_INTERVAL", "300"))
# How long the audit evaluator looks back when checking propagation and
# join completeness on each tick.
AUDIT_EVALUATOR_LOOKBACK_S = int(os.getenv("AUDIT_EVALUATOR_LOOKBACK_S", "1800"))

# Collection-staleness detector (petrosa-data-manager#300). The existing
# consume-without-persist detector only catches messages RECEIVED from NATS
# but not persisted — it cannot see an upstream producer (CIO/tradeengine)
# going silent, because zero receipts minus zero persists is zero delta,
# not a trip. This detector instead tracks the newest document's age per
# collection so a stalled audit-persistence pipeline (source went silent,
# not a data-manager bug) surfaces within one tick interval instead of
# being found by a manual Atlas audit weeks later.
AUDIT_STALENESS_COLLECTIONS = tuple(
    name.strip()
    for name in os.getenv(
        "AUDIT_STALENESS_COLLECTIONS", "cio_decisions,execution_events,pnl_events"
    ).split(",")
    if name.strip()
)
# Threshold (seconds) beyond which a monitored collection's newest document
# is considered stale. Default 1h — comfortably above normal quiet periods
# (e.g. a ranging market with no EXECUTE decisions for a few ticks) while
# still catching a multi-day freeze like #300 well before manual discovery.
AUDIT_STALENESS_THRESHOLD_S = int(os.getenv("AUDIT_STALENESS_THRESHOLD_S", "3600"))

# MongoDB logical-data-size gauge (data-manager#248) — producer half of the
# Atlas M0 data-size leading-indicator alert (petrosa_k8s#905, dm#244 AC6).
# Atlas M0's 512 MiB quota gates on LOGICAL uncompressed `dbStats().dataSize`
# (NOT the compressed storageSize), which is exactly how all four 2026 P0
# quota breaches were diagnosed. data-manager is already scraped into Grafana
# Cloud on :9090, so registering this gauge on the existing registry makes it
# flow with no new remote-write/Alloy/secret/CronJob. Defaults true.
ENABLE_MONGO_DATA_SIZE_GAUGE = (
    os.getenv("ENABLE_MONGO_DATA_SIZE_GAUGE", "true").lower() == "true"
)
# Refresh cadence (seconds) for the mongo data-size gauge. 60s is cheap: one
# dbStats command per application DB per minute, well below scrape frequency.
MONGO_DATA_SIZE_REFRESH_INTERVAL = int(
    os.getenv("MONGO_DATA_SIZE_REFRESH_INTERVAL", "60")
)
# Optional comma-separated EXTRA database names to sample beyond the connected
# DB (petrosa_data_manager, which dominates ~99% of logical size). The gauge
# always samples the connected DB; these are added by explicit name via
# dbStats. Left empty by default — deliberately NOT using listDatabases, which
# the app's Mongo credential cannot run (see mongo_data_size_gauge docstring).
MONGO_DATA_SIZE_DATABASES = tuple(
    name.strip()
    for name in os.getenv("MONGO_DATA_SIZE_DATABASES", "").split(",")
    if name.strip()
)

# Scheduling Configuration
AUDIT_INTERVAL = int(os.getenv("AUDIT_INTERVAL", "300"))  # 5 minutes
ANALYTICS_INTERVAL = int(os.getenv("ANALYTICS_INTERVAL", "900"))  # 15 minutes
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))  # 1 minute
INITIAL_STARTUP_DELAY = int(
    os.getenv("INITIAL_STARTUP_DELAY", "60")
)  # 1 minute delay before background cycles

# API Configuration
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
API_WORKERS = int(os.getenv("API_WORKERS", "4"))

# Binance API Configuration (for backfilling)
BINANCE_API_BASE_URL = os.getenv("BINANCE_API_BASE_URL", "https://api.binance.com")
BINANCE_FAPI_BASE_URL = os.getenv("BINANCE_FAPI_BASE_URL", "https://fapi.binance.com")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_RATE_LIMIT = int(os.getenv("BINANCE_RATE_LIMIT", "1200"))  # Requests per minute

# Data Processing Configuration
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "1000"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "10"))
MESSAGE_QUEUE_SIZE = int(os.getenv("MESSAGE_QUEUE_SIZE", "10000"))

# Gap Detection Configuration
GAP_TOLERANCE_SECONDS = int(os.getenv("GAP_TOLERANCE_SECONDS", "60"))
MIN_GAP_SIZE_SECONDS = int(os.getenv("MIN_GAP_SIZE_SECONDS", "120"))

# Analytics Configuration
DEFAULT_VOLATILITY_WINDOW = int(os.getenv("DEFAULT_VOLATILITY_WINDOW", "30"))
DEFAULT_VOLUME_WINDOW = int(os.getenv("DEFAULT_VOLUME_WINDOW", "24"))
DEFAULT_TREND_WINDOW = int(os.getenv("DEFAULT_TREND_WINDOW", "20"))

# Health Check Configuration
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8080"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

# OpenTelemetry Configuration
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() == "true"
# Default to common Grafana Alloy endpoint if not set
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://grafana-alloy.observability.svc.cluster.local:4317",
)
OTEL_SERVICE_NAME = SERVICE_NAME

# Supported trading pairs
SUPPORTED_PAIRS = os.getenv(
    "SUPPORTED_PAIRS",
    "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT,LINKUSDT,LTCUSDT,XRPUSDT",
).split(",")

# Candle Database Configuration
CANDLE_DATABASE_TYPE = os.getenv("CANDLE_DATABASE_TYPE", "mongodb").lower()
if CANDLE_DATABASE_TYPE not in {"mongodb", "mysql"}:
    logging.error(
        "Unsupported CANDLE_DATABASE_TYPE=%r; using mongodb", CANDLE_DATABASE_TYPE
    )
    CANDLE_DATABASE_TYPE = "mongodb"

KLINE_WRITER_VERSION = "data-manager"
KLINE_WRITER_SOURCE = "data-manager-backfill"

# Supported timeframes for candles
SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

# Timeframes the *execution* path actually requests. Sourced from the shared
# `SUPPORTED_INTERVALS` configmap key (k8s/shared/configmaps/petrosa-common-config.yaml)
# so the warm-up/readiness grid matches what petrosa-bot-ta-analysis really asks
# for, instead of the broader historical `SUPPORTED_TIMEFRAMES` list above.
SUPPORTED_INTERVALS = [
    tf.strip()
    for tf in os.getenv("SUPPORTED_INTERVALS", "5m,15m,30m,1h,1d").split(",")
    if tf.strip()
]

# --- Candle-store cutover safety (data-manager#275) -------------------------
# Minimum number of candles every `candles_{pair}_{timeframe}` collection must
# hold before MongoDB may be promoted to the primary execution candle store
# (#274 AC1). The value is the contract produced by #276
# (docs/candle-consumer-retention-contract.md): true max strategy lookback is
# 265 candles (minervini_trend_template), 1.5x safety margin -> 400. Retention
# (#274 AC3) must be at least as deep or a freshly cut-over collection would
# shed candles it just backfilled.
CANDLE_WARMUP_MIN_CANDLES = int(os.getenv("CANDLE_WARMUP_MIN_CANDLES", "400"))

# How stale the newest candle in a collection may be, expressed as a multiple
# of the collection's own timeframe, before readiness fails. 3 intervals
# tolerates one missed extractor tick plus scheduling jitter without declaring
# a healthy collection cold.
CANDLE_WARMUP_FRESHNESS_INTERVALS = float(
    os.getenv("CANDLE_WARMUP_FRESHNESS_INTERVALS", "3")
)

# Warm-up backfill batch size (documents per MongoDB insert_many call).
CANDLE_WARMUP_BATCH_SIZE = int(os.getenv("CANDLE_WARMUP_BATCH_SIZE", "500"))

# Self-bounding trim for the warm-up job. Per the standing operator rule
# ("never leave anything writing to MongoDB without a TTL AND an easy
# off-flag" — four Atlas M0 quota P0s: k8s#783/#819/#881/#899), the backfill
# job caps every collection it touches at
# CANDLE_WARMUP_MIN_CANDLES * CANDLE_WARMUP_TRIM_FACTOR documents so repeated
# runs cannot grow the namespace. This is the job's own bound; the general
# `candles_*` retention job is #274 AC3.
CANDLE_WARMUP_TRIM_ENABLED = (
    os.getenv("CANDLE_WARMUP_TRIM_ENABLED", "true").lower() == "true"
)
CANDLE_WARMUP_TRIM_FACTOR = float(os.getenv("CANDLE_WARMUP_TRIM_FACTOR", "1.5"))

# --- Continuous warm-up backfill scheduler (#319) ------------------------
# #275 shipped the warm-up backfill as a one-shot cutover tool. Gaps that form
# AFTER the cutover were never refilled, so collections drift stale. The
# scheduler re-runs the very same `run_backfill()` on a fixed cadence from
# inside the long-lived data-manager process (leader-elected, so N replicas
# still produce one writer).
#
# Off by default: it writes to MongoDB, and the standing operator rule after
# four Atlas M0 quota P0s (k8s#783/#819/#881/#899) is that no new Mongo writer
# is enabled implicitly. Enable per-environment via the ConfigMap. The write is
# bounded regardless — the backfill trims every collection it touches back to
# CANDLE_WARMUP_MIN_CANDLES * CANDLE_WARMUP_TRIM_FACTOR.
ENABLE_CANDLE_WARMUP_SCHEDULER = (
    os.getenv("ENABLE_CANDLE_WARMUP_SCHEDULER", "false").lower() == "true"
)

# Seconds between warm-up cycles. 3600 (hourly) comfortably beats the tightest
# freshness budget in the grid (5m x CANDLE_WARMUP_FRESHNESS_INTERVALS = 15m)
# for depth maintenance, while the per-cycle readiness gate means a healthy
# grid costs 50 cheap count/latest queries and zero writes.
CANDLE_WARMUP_SCHEDULER_INTERVAL = int(
    os.getenv("CANDLE_WARMUP_SCHEDULER_INTERVAL", "3600")
)

# Delay before the first cycle, so the pod passes its readiness probe and the
# database connections settle before a 50-collection sweep starts.
CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY = int(
    os.getenv("CANDLE_WARMUP_SCHEDULER_INITIAL_DELAY", "300")
)

# Backoff applied after a cycle raises, so a persistently broken backend is
# retried politely instead of hot-looping.
CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF = int(
    os.getenv("CANDLE_WARMUP_SCHEDULER_ERROR_BACKOFF", "300")
)

# AC3 — no empty-read window. During the cutover the candle read path falls
# back to the non-primary backend whenever the primary returns an empty or
# short result, so execution never sees a starved candle window while Mongo is
# warming (or while MySQL is catching up after a rollback). Kill-switch:
# CANDLE_READ_FALLBACK_ENABLED=false restores single-backend reads.
CANDLE_READ_FALLBACK_ENABLED = (
    os.getenv("CANDLE_READ_FALLBACK_ENABLED", "true").lower() == "true"
)
CANDLE_READINESS_COLLECTION_TIMEOUT_SECONDS = float(
    os.getenv("CANDLE_READINESS_COLLECTION_TIMEOUT_SECONDS", "5")
)

# AC4 — rollback path. With dual-write enabled, candle writes land in BOTH
# backends, so reverting CANDLE_DATABASE_TYPE to `mysql` after the flip cannot
# leave a hole in MySQL for the period MongoDB was primary. Default OFF: it
# doubles the Mongo candle write volume, so it is switched on deliberately for
# the cutover window (and requires #274 AC3 retention to stay bounded beyond
# the warm-up job's own trim).
CANDLE_DUAL_WRITE_ENABLED = (
    os.getenv("CANDLE_DUAL_WRITE_ENABLED", "false").lower() == "true"
)

# Leader Election Configuration
ENABLE_LEADER_ELECTION = os.getenv("ENABLE_LEADER_ELECTION", "true").lower() == "true"
LEADER_ELECTION_HEARTBEAT_INTERVAL = int(
    os.getenv("LEADER_ELECTION_HEARTBEAT_INTERVAL", "10")
)  # seconds
LEADER_ELECTION_TIMEOUT = int(os.getenv("LEADER_ELECTION_TIMEOUT", "30"))  # seconds

# Auto-Backfill Configuration
ENABLE_AUTO_BACKFILL = os.getenv("ENABLE_AUTO_BACKFILL", "false").lower() == "true"
MIN_AUTO_BACKFILL_GAP = int(
    os.getenv("MIN_AUTO_BACKFILL_GAP", "3600")
)  # seconds (1 hour)
MAX_AUTO_BACKFILL_JOBS = int(
    os.getenv("MAX_AUTO_BACKFILL_JOBS", "5")
)  # concurrent jobs

# Backfill request queue (petrosa-data-manager#320)
# In-memory queue used when the backfill orchestrator is unavailable.
BACKFILL_QUEUE_MAX_SIZE = int(
    os.getenv("BACKFILL_QUEUE_MAX_SIZE", "100")
)  # max queued requests
BACKFILL_QUEUE_MAX_RETRIES = int(
    os.getenv("BACKFILL_QUEUE_MAX_RETRIES", "3")
)  # max retries per request on flush
BACKFILL_QUEUE_FLUSH_INTERVAL = int(
    os.getenv("BACKFILL_QUEUE_FLUSH_INTERVAL", "60")
)  # seconds between flush attempts

# Duplicate Handling Configuration
ENABLE_DUPLICATE_REMOVAL = (
    os.getenv("ENABLE_DUPLICATE_REMOVAL", "false").lower() == "true"
)
DUPLICATE_RESOLUTION_STRATEGY = os.getenv(
    "DUPLICATE_RESOLUTION_STRATEGY", "keep_newest"
)  # keep_newest, keep_oldest, manual

# Connection Management Configuration
DB_HEALTH_CHECK_INTERVAL = int(os.getenv("DB_HEALTH_CHECK_INTERVAL", "30"))  # seconds
DB_RECONNECT_MAX_ATTEMPTS = int(os.getenv("DB_RECONNECT_MAX_ATTEMPTS", "10"))
DB_RECONNECT_BACKOFF_BASE = int(
    os.getenv("DB_RECONNECT_BACKOFF_BASE", "2")
)  # exponential backoff
DB_CONNECTION_TIMEOUT = int(os.getenv("DB_CONNECTION_TIMEOUT", "30"))  # seconds

# API Limits Configuration
API_MAX_PAGE_SIZE = int(os.getenv("API_MAX_PAGE_SIZE", "10000"))
API_DEFAULT_PAGE_SIZE = int(os.getenv("API_DEFAULT_PAGE_SIZE", "100"))
API_MAX_BATCH_SIZE = int(os.getenv("API_MAX_BATCH_SIZE", "5000"))
API_QUERY_TIMEOUT = int(os.getenv("API_QUERY_TIMEOUT", "30"))  # seconds

# Raw Query Limits Configuration
RAW_QUERY_TIMEOUT = int(os.getenv("RAW_QUERY_TIMEOUT", "60"))  # seconds
RAW_QUERY_MAX_RESULTS = int(os.getenv("RAW_QUERY_MAX_RESULTS", "100000"))
RAW_QUERY_ENABLED = os.getenv("RAW_QUERY_ENABLED", "true").lower() == "true"

# Logging Configuration
LOG_REQUEST_DETAILS = os.getenv("LOG_REQUEST_DETAILS", "true").lower() == "true"
LOG_RESPONSE_DETAILS = os.getenv("LOG_RESPONSE_DETAILS", "true").lower() == "true"
LOG_QUERY_DETAILS = os.getenv("LOG_QUERY_DETAILS", "false").lower() == "true"

# Schema Registry Configuration
SCHEMA_VALIDATION_ENABLED = (
    os.getenv("SCHEMA_VALIDATION_ENABLED", "true").lower() == "true"
)
SCHEMA_STRICT_MODE = os.getenv("SCHEMA_STRICT_MODE", "false").lower() == "true"
SCHEMA_CACHE_TTL = int(os.getenv("SCHEMA_CACHE_TTL", "300"))  # seconds
SCHEMA_AUTO_REGISTER = os.getenv("SCHEMA_AUTO_REGISTER", "false").lower() == "true"
SCHEMA_MAX_VERSIONS = int(os.getenv("SCHEMA_MAX_VERSIONS", "10"))
SCHEMA_COMPATIBILITY_MODE = os.getenv(
    "SCHEMA_COMPATIBILITY_MODE", "BACKWARD"
)  # BACKWARD, FORWARD, FULL, NONE

# Streaming Gap Detection Configuration (data-manager#322)
ENABLE_STREAMING_GAP_DETECTION = (
    os.getenv("ENABLE_STREAMING_GAP_DETECTION", "true").lower() == "true"
)
STREAMING_GAP_DETECTION_INTERVAL = int(
    os.getenv("STREAMING_GAP_DETECTION_INTERVAL", "60")
)  # seconds: how often to log heartbeat
STREAMING_GAP_DETECTION_REPORT_COOLDOWN = int(
    os.getenv("STREAMING_GAP_DETECTION_REPORT_COOLDOWN", "30")
)  # seconds: dedup window per (symbol, timeframe)
