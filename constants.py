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
NATS_CONSUMER_SUBJECT = os.getenv("NATS_CONSUMER_SUBJECT", "binance.futures.websocket.data")
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

# Scheduling Configuration
AUDIT_INTERVAL = int(os.getenv("AUDIT_INTERVAL", "300"))  # 5 minutes
ANALYTICS_INTERVAL = int(os.getenv("ANALYTICS_INTERVAL", "900"))  # 15 minutes
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))  # 1 minute

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
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_SERVICE_NAME = SERVICE_NAME

# Supported trading pairs
SUPPORTED_PAIRS = os.getenv(
    "SUPPORTED_PAIRS", "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT"
).split(",")

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
MAX_AUTO_BACKFILL_JOBS = int(os.getenv("MAX_AUTO_BACKFILL_JOBS", "5"))  # concurrent jobs

# Duplicate Handling Configuration
ENABLE_DUPLICATE_REMOVAL = (
    os.getenv("ENABLE_DUPLICATE_REMOVAL", "false").lower() == "true"
)
DUPLICATE_RESOLUTION_STRATEGY = os.getenv(
    "DUPLICATE_RESOLUTION_STRATEGY", "keep_newest"
)  # keep_newest, keep_oldest, manual

