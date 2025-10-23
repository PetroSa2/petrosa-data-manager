"""
FastAPI application factory.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

import constants
from data_manager.api.middleware import MetricsMiddleware, RequestLoggerMiddleware
from data_manager.api.routes import (
    analysis,
    anomalies,
    backfill,
    catalog,
    config,
    data,
    generic,
    health,
    raw,
    schemas,
)

logger = logging.getLogger(__name__)

# Global database manager reference (will be set by main app)
db_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting Data Manager API")
    # Startup logic here
    yield
    # Shutdown logic here
    logger.info("Shutting down Data Manager API")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="Petrosa Data Manager API",
        description="Data integrity, intelligence, and distribution hub",
        version=constants.SERVICE_VERSION,
        lifespan=lifespan,
    )

    # Add custom middleware (order matters - first added is outermost)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestLoggerMiddleware)

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount Prometheus metrics
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # Include routers
    app.include_router(health.router, prefix="/health", tags=["Health"])
    app.include_router(data.router, prefix="/data", tags=["Data"])
    app.include_router(analysis.router, prefix="/analysis", tags=["Analysis"])
    app.include_router(catalog.router, prefix="/catalog", tags=["Catalog"])
    app.include_router(backfill.router, prefix="/backfill", tags=["Backfill"])
    app.include_router(anomalies.router, prefix="/anomalies", tags=["Anomalies"])
    app.include_router(config.router, tags=["Configuration"])

    # New API routes
    app.include_router(generic.router, tags=["Generic CRUD"])
    app.include_router(raw.router, tags=["Raw Queries"])
    app.include_router(schemas.router, tags=["Schema Registry"])

    # Root endpoint
    @app.get("/")
    async def root():
        return {
            "service": "petrosa-data-manager",
            "version": constants.SERVICE_VERSION,
            "status": "operational",
        }

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(exc)},
        )

    return app
