"""
Constants and configuration for the Petrosa Data Manager service.
"""

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
MONGODB_URL = os.getenv(
    "MONGODB_URL",
    f"mongodb://{MONGODB_USER}:{MONGODB_PASSWORD}@{MONGODB_HOST}:{MONGODB_PORT}/{MONGODB_DB}"
    if MONGODB_USER
    else f"mongodb://{MONGODB_HOST}:{MONGODB_PORT}/{MONGODB_DB}",
)

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
CANDLE_DATABASE_TYPE = os.getenv(
    "CANDLE_DATABASE_TYPE",
    os.getenv("DB_ADAPTER", os.getenv("EXTRACTOR_DB_ADAPTER", "mongodb")),
).lower()

# Supported timeframes for candles
SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

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
