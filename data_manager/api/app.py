"""
FastAPI application factory.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

import constants
from data_manager.api.gateway_auth import require_service
from data_manager.api.middleware import MetricsMiddleware, RequestLoggerMiddleware
from data_manager.api.routes import (
    analysis,
    anomalies,
    audit,
    backfill,
    breaches,
    catalog,
    characterizations,
    cio_state,
    config,
    config_rate_limits,
    dashboard,
    data,
    dr_status,
    drawdown,
    envelopes,
    fidelity,
    generic,
    health,
    ingest,
    leases,
    ledger,
    leverage_bounds,
    lifecycle,
    pnl,
    portfolio_state,
    raw,
    risk,
    schemas,
    scorecard,
    service_config,
    signals,
    strategies,
    strategy_lifecycle,
    strategy_net_r,
    strategy_performance,
    strategy_timeline,
    system_trades,
    trading_state,
)
from data_manager.maintenance import klines_completeness as _klines_completeness

try:
    from petrosa_otel import config_rate_limit_middleware
except ImportError:
    config_rate_limit_middleware = None

logger = logging.getLogger(__name__)

# Global database manager reference: set only through publish_database() once it is initialized.
db_manager = None
_database_ready: asyncio.Event | None = None
_database_ready_loop: asyncio.AbstractEventLoop | None = None


def _database_ready_event() -> asyncio.Event:
    """The "database published" event of the running loop (created lazily, per loop)."""
    global _database_ready, _database_ready_loop
    loop = asyncio.get_running_loop()
    if _database_ready is None or _database_ready_loop is not loop:
        _database_ready = asyncio.Event()
        _database_ready_loop = loop
        if db_manager is not None:
            _database_ready.set()
    return _database_ready


def publish_database(manager: Any) -> None:
    """Make a fully initialized manager visible to the API and wake everything waiting for it."""
    global db_manager
    db_manager = manager
    if _database_ready is not None:
        _database_ready.set()


def unpublish_database(manager: Any = None) -> None:
    """Withdraw the published manager (only ``manager``, when given), so waiters block again."""
    global db_manager
    if manager is not None and db_manager is not manager:
        return
    db_manager = None
    if _database_ready is not None:
        _database_ready.clear()


async def wait_for_database() -> Any:
    """Return the published manager, waiting until one is published (#829: no half-initialized use)."""
    while True:
        await _database_ready_event().wait()
        if db_manager is not None:
            return db_manager
        _database_ready_event().clear()


leader_election = None
audit_scheduler = None
report_precomputer = None

_RATE_LIMIT_BOOKKEEPING_PATHS = frozenset(
    {
        "/api/v1/config/rate-limits/check",
        "/api/v1/config/rate-limits/record",
    }
)


async def _config_rate_limit_middleware(request, call_next):
    """Keep rate-limit bookkeeping from recursively rate limiting itself."""
    if request.url.path in _RATE_LIMIT_BOOKKEEPING_PATHS:
        return await call_next(request)
    return await config_rate_limit_middleware(request, call_next)


async def _allowlist_strategy_ids(adapter: Any) -> list[str]:
    """The runtime allowlist (``enabled_strategies`` in the application config)."""
    config = await adapter.get_app_config() or {}
    return list((config.get("parameters") or {}).get("enabled_strategies") or [])


async def _traded_strategy_ids(adapter: Any) -> list[str]:
    """Strategies present in the execution events that the performance stats are computed from.

    Reads through the same shared, versioned cache as the refresh's computes, so it adds no extra
    database round trip within a cache period.
    """
    from data_manager.api.routes.analysis import _load_execution_events

    rows, _latest = await _load_execution_events(adapter)
    return [str(row["strategy_id"]) for row in rows if row.get("strategy_id")]


#: Seed sources beyond the registry. A strategy outside the registry (e.g. a realtime strategy) that
#: trades, or is on the runtime allowlist, is refreshed on every pod from the first pass, instead of
#: answering ``warming`` on each pod until that pod is asked for it (#833).
_EXTRA_SEED_SOURCES = (
    ("allowlist", _allowlist_strategy_ids),
    ("execution_events", _traded_strategy_ids),
)


async def _configured_strategy_ids() -> list[str]:
    """Seed for the performance snapshot refresh: registry, runtime allowlist and traded strategies.

    The registry read is the base seed. Each extra source is best effort: its failure is a WARNING
    and the pass still seeds everything else.
    """
    adapter = getattr(db_manager, "mongodb_adapter", None)
    if adapter is None:
        return []
    strategy_ids = list(await adapter.list_all_strategy_ids())
    for name, source in _EXTRA_SEED_SOURCES:
        try:
            strategy_ids.extend(await source(adapter))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "performance_snapshot: seed source=%s outcome=error exception=%s",
                name,
                type(exc).__name__,
            )
    return list(dict.fromkeys(s for s in strategy_ids if s))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting Data Manager API")
    import asyncio

    from data_manager.api.routes.analysis import compute_strategy_performance
    from data_manager.api.routes.trading_state import mysql_copy_worker
    from data_manager.services import performance_snapshots

    worker = asyncio.create_task(mysql_copy_worker())
    snapshots = asyncio.create_task(
        performance_snapshots.supervise(
            performance_snapshots.store,
            compute_strategy_performance,
            _configured_strategy_ids,
        )
    )
    try:
        yield
    finally:
        worker.cancel()
        snapshots.cancel()
        await asyncio.gather(worker, snapshots, return_exceptions=True)
        logger.info("Shutting down Data Manager API")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""

    app = FastAPI(
        title="Petrosa Data Manager API",
        description="Data integrity, intelligence, and distribution hub",
        version=constants.SERVICE_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(require_service)],
    )

    # Add custom middleware (order matters - first added is outermost)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestLoggerMiddleware)

    # Register configuration rate limit middleware
    if config_rate_limit_middleware:
        app.middleware("http")(_config_rate_limit_middleware)
        logger.info("✅ Configuration rate limit middleware registered")

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in os.environ.get("DM_CORS_ORIGINS", "").split(",")
            if origin.strip()
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Legacy configuration path redirect middleware (Internal redirect)
    @app.middleware("http")
    async def redirect_legacy_config_paths(request, call_next):
        """
        Internally redirect legacy config paths to versioned ones.
        Example: /config/application -> /api/v1/config/application
        Also handles redundant version prefixes for robustness.
        Example: /api/v1/api/v1/config -> /api/v1/config
        """
        path = request.url.path
        if path.startswith("/config/") and not path.startswith("/api/v1/config/"):
            new_path = f"/api/v1{path}"
            logger.info(f"Redirecting legacy config path: {path} -> {new_path}")
            # Modify the path in the scope for internal routing
            request.scope["path"] = new_path
        elif "/api/v1/api/v1/" in path:
            new_path = path.replace("/api/v1/api/v1/", "/api/v1/")
            logger.info(f"Fixing redundant version prefix: {path} -> {new_path}")
            # Modify the path in the scope for internal routing
            request.scope["path"] = new_path

        return await call_next(request)

    # Mount Prometheus metrics
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # Include routers
    app.include_router(health.router, prefix="/health", tags=["Health"])
    app.include_router(config_rate_limits.router)
    app.include_router(service_config.router)
    app.include_router(config.router, tags=["Configuration"])
    app.include_router(raw.router, prefix="/api/v1/raw", tags=["Raw Queries"])
    app.include_router(
        schemas.router, prefix="/api/v1/registry", tags=["Schema Registry"]
    )
    # Legacy schema registry routes for backward compatibility
    app.include_router(
        schemas.router, prefix="/schemas", tags=["Schema Registry (legacy)"]
    )
    app.include_router(data.router, prefix="/data", tags=["Data"])
    app.include_router(scorecard.router, prefix="/analysis", tags=["Analysis"])
    app.include_router(scorecard.router, prefix="/api/v1/analysis", tags=["Analysis"])
    app.include_router(analysis.router, prefix="/analysis", tags=["Analysis"])
    app.include_router(analysis.router, prefix="/api/v1/analysis", tags=["Analysis"])
    app.include_router(
        analysis.calibration_router, prefix="/api/v1", tags=["Calibration"]
    )
    # The keep/kill input of the CIO (petrosa-data-manager#468): net R per closed round of each strategy
    app.include_router(strategy_net_r.router, prefix="/analysis", tags=["Analysis"])
    app.include_router(
        strategy_net_r.router, prefix="/api/v1/analysis", tags=["Analysis"]
    )
    app.include_router(catalog.router, prefix="/catalog", tags=["Catalog"])
    app.include_router(backfill.router, prefix="/backfill", tags=["Backfill"])
    app.include_router(anomalies.router, prefix="/anomalies", tags=["Anomalies"])
    app.include_router(
        characterizations.router,
        prefix="/api/v1",
        tags=["Characterizations"],
    )
    app.include_router(
        strategy_timeline.router,
        prefix="/api/v1",
        tags=["Strategy Timeline"],
    )
    app.include_router(
        strategy_lifecycle.router,
        prefix="/api/v1",
        tags=["Strategy Lifecycle"],
    )
    app.include_router(cio_state.router, tags=["CIO Auto Resume"])
    app.include_router(pnl.router, prefix="/api/v1", tags=["P&L"])
    app.include_router(risk.router, prefix="/api/v1/risk", tags=["Risk"])
    # System trade audit trail from execution_events (#529).
    app.include_router(system_trades.router, prefix="/api/v1", tags=["System Trades"])
    app.include_router(
        strategy_performance.router, prefix="/api/v1", tags=["Strategy Performance"]
    )
    # Cross-service decision audit-trail (#605 P4.5).
    app.include_router(audit.router, prefix="/api/v1", tags=["Audit Trail"])
    # Lifecycle reconstruction join (#603 P4.3).
    app.include_router(lifecycle.router, prefix="/api/v1", tags=["Lifecycle"])
    # Portfolio drawdown vs characterization envelope (#602 P4.2).
    app.include_router(drawdown.router, prefix="/api/v1", tags=["Portfolio"])
    # Strategy-fidelity evaluator (#594 P2.3).
    app.include_router(fidelity.router, prefix="/api/v1", tags=["Strategy Fidelity"])
    # Portfolio state-at-time-T query (#604 P4.4).
    app.include_router(
        portfolio_state.router, prefix="/api/v1", tags=["Portfolio State"]
    )

    # Operator dashboard backend API surface (#644 P5.1a). Thin façade over
    # the existing service-layer code that powers /api/v1. Mounted at
    # /api/dashboard so the SPA contract is decoupled from /api/v1.
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
    # leverage_bounds routes already include the /api/dashboard prefix in-path
    # so they're registered without a router-level prefix.
    app.include_router(leverage_bounds.router, tags=["Dashboard"])
    # dr_status route also carries the /api/dashboard prefix in-path
    # (#743 / P9-AC5.c).
    app.include_router(dr_status.router, tags=["Dashboard"])
    # Operator envelope-approval workflow (#187, P4.6-AC1 / FR62) — routes
    # embed the /api/envelopes prefix in-path.
    app.include_router(envelopes.router, tags=["Envelopes"])

    # P4.6-AC6.c / #194 — drawdown breach read API.
    app.include_router(
        breaches.router, prefix="/api/breaches", tags=["Drawdown Breaches"]
    )

    # Strategy registry (#195, FR54) — POST/GET /api/strategies. Routes embed
    # the prefix in-path.
    app.include_router(strategies.router, tags=["Strategies"])

    # New API routes
    app.include_router(leases.router, tags=["Leases"])
    app.include_router(ledger.router, tags=["Exchange Ledger"])
    app.include_router(trading_state.router, tags=["Trading State"])
    app.include_router(ingest.router)
    app.include_router(signals.router, tags=["Signals"])
    app.include_router(generic.router, tags=["Generic CRUD"])

    # Root endpoint
    @app.get("/")
    async def root():
        return {
            "service": "petrosa-data-manager",
            "version": constants.SERVICE_VERSION,
            "status": "operational",
        }

    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(exc)},
        )

    return app
