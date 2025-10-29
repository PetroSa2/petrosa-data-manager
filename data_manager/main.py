"""
Main application entry point for Petrosa Data Manager.
"""

import asyncio
import logging
import signal
import sys
from typing import TYPE_CHECKING

import structlog
import uvicorn
from prometheus_client import start_http_server

import constants
import otel_init
from data_manager.api.app import create_app
from data_manager.consumer.market_data_consumer import MarketDataConsumer
from data_manager.db.database_manager import DatabaseManager

if TYPE_CHECKING:
    from data_manager.backfiller.orchestrator import BackfillOrchestrator
    from data_manager.leader_election import LeaderElectionManager

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, constants.LOG_LEVEL.upper()),
)

logger = logging.getLogger(__name__)


class DataManagerApp:
    """Main application coordinator."""

    def __init__(self):
        self.db_manager: DatabaseManager | None = None
        self.consumer: MarketDataConsumer | None = None
        self.api_server_task: asyncio.Task | None = None
        self.leader_election: LeaderElectionManager | None = None
        self.backfill_orchestrator: BackfillOrchestrator | None = None
        self.running = False
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start all application components."""
        logger.info(
            "Starting Petrosa Data Manager",
            extra={
                "version": constants.SERVICE_VERSION,
                "environment": constants.ENVIRONMENT,
            },
        )

        # Start Prometheus metrics server
        try:
            start_http_server(constants.METRICS_PORT)
            logger.info(
                f"Prometheus metrics server started on port {constants.METRICS_PORT}"
            )
        except Exception as e:
            logger.error(f"Failed to start metrics server: {e}")

        # Initialize database connections
        try:
            self.db_manager = DatabaseManager()
            await self.db_manager.initialize()
            logger.info("Database connections initialized successfully")
        except Exception as e:
            logger.warning(
                f"Failed to initialize databases: {e}. "
                "Service will run in limited mode (NATS consumer only)."
            )
            # Continue without database - will be limited functionality
            self.db_manager = None

        # Initialize leader election if enabled and database available
        if (
            constants.ENABLE_LEADER_ELECTION
            and self.db_manager
            and self.db_manager.mongodb_adapter
        ):
            try:
                from data_manager.leader_election import LeaderElectionManager

                self.leader_election = LeaderElectionManager()
                await self.leader_election.initialize(
                    self.db_manager.mongodb_adapter.client
                )
                await self.leader_election.start()
                logger.info(
                    f"Leader election initialized: "
                    f"is_leader={self.leader_election.is_leader}, "
                    f"pod_id={self.leader_election.pod_id}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to initialize leader election: {e}", exc_info=True
                )
                self.leader_election = None

        # Initialize and start NATS consumer
        if constants.ENABLE_API or True:  # Always start consumer for now
            self.consumer = MarketDataConsumer(db_manager=self.db_manager)
            if await self.consumer.start():
                logger.info("Market data consumer started successfully")
            else:
                logger.error("Failed to start market data consumer")

        # Initialize backfill orchestrator
        if self.db_manager and constants.ENABLE_BACKFILLER:
            from data_manager.backfiller.orchestrator import BackfillOrchestrator

            self.backfill_orchestrator = BackfillOrchestrator(self.db_manager)
            logger.info("Backfill orchestrator initialized")
        else:
            self.backfill_orchestrator = None

        # Start FastAPI server in background
        if constants.ENABLE_API:
            self.api_server_task = asyncio.create_task(self._run_api_server())
            logger.info("API server task created")

        # Start background workers
        asyncio.create_task(self._run_auditor())
        asyncio.create_task(self._run_analytics())

        self.running = True
        logger.info("All components started successfully")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop all application components."""
        logger.info("Stopping Petrosa Data Manager")
        self.running = False

        # Stop leader election
        if self.leader_election:
            await self.leader_election.stop()
            logger.info("Leader election stopped")

        # Stop consumer
        if self.consumer:
            await self.consumer.stop()
            logger.info("Consumer stopped")

        # Stop API server
        if self.api_server_task:
            self.api_server_task.cancel()
            try:
                await self.api_server_task
            except asyncio.CancelledError:
                pass
            logger.info("API server stopped")

        # Shutdown database connections
        if self.db_manager:
            await self.db_manager.shutdown()
            logger.info("Database connections closed")

        logger.info("Petrosa Data Manager stopped")

    async def _run_api_server(self) -> None:
        """Run FastAPI server."""
        logger.info(f"Starting API server on {constants.API_HOST}:{constants.API_PORT}")

        # Create app and set database manager reference
        app = create_app()
        from data_manager import api
        from data_manager.api.routes import backfill, config

        api.app.db_manager = self.db_manager
        backfill.backfill_orchestrator = getattr(self, "backfill_orchestrator", None)
        config.set_database_manager(self.db_manager)

        config = uvicorn.Config(
            app,
            host=constants.API_HOST,
            port=constants.API_PORT,
            log_level=constants.LOG_LEVEL.lower(),
            access_log=True,
        )
        server = uvicorn.Server(config)

        try:
            await server.serve()
        except asyncio.CancelledError:
            logger.info("API server task cancelled")

    async def _run_auditor(self) -> None:
        """Run data auditor in background."""
        if not constants.ENABLE_AUDITOR:
            logger.info("Auditor is disabled")
            return

        if not self.db_manager:
            logger.warning("Auditor requires database, but db_manager not available")
            return

        # Check database health before starting
        if not self.db_manager.is_healthy():
            logger.warning(
                "Auditor not started: Database connections not healthy. "
                "This is expected if databases are not yet configured."
            )
            return

        logger.info("Starting auditor background worker")

        # Import here to avoid circular dependency
        from data_manager.auditor.scheduler import AuditScheduler

        try:
            audit_scheduler = AuditScheduler(
                self.db_manager,
                leader_election=self.leader_election,
                backfill_orchestrator=self.backfill_orchestrator,
            )
            await audit_scheduler.start()
        except Exception as e:
            logger.error(f"Error in auditor: {e}", exc_info=True)

        logger.info("Auditor stopped")

    async def _run_analytics(self) -> None:
        """Run analytics engine in background."""
        if not constants.ENABLE_ANALYTICS:
            logger.info("Analytics is disabled")
            return

        if not self.db_manager:
            logger.warning("Analytics requires database, but db_manager not available")
            return

        # Check database health before starting
        if not self.db_manager.is_healthy():
            logger.warning(
                "Analytics not started: Database connections not healthy. "
                "This is expected if databases are not yet configured."
            )
            return

        logger.info("Starting analytics background worker")

        # Import here to avoid circular dependency
        from data_manager.analytics.scheduler import AnalyticsScheduler

        try:
            analytics_scheduler = AnalyticsScheduler(self.db_manager)
            await analytics_scheduler.start()
        except Exception as e:
            logger.error(f"Error in analytics: {e}", exc_info=True)

        logger.info("Analytics stopped")

    def shutdown(self, signum, frame):
        """Handle shutdown signal."""
        logger.info(f"Received signal {signum}, initiating shutdown")
        self._shutdown_event.set()


async def main():
    """Main application entry point."""
    # 1. Setup OpenTelemetry FIRST (before any logging configuration)
    try:
        if constants.OTEL_ENABLED:
            logger.info(
                f"Initializing OpenTelemetry with endpoint: {constants.OTEL_EXPORTER_OTLP_ENDPOINT}"
            )
            # Initialize OpenTelemetry using local otel_init module
            otel_init.init_telemetry()
            logger.info("OpenTelemetry initialized successfully")
        else:
            logger.warning("OpenTelemetry is disabled (OTEL_ENABLED=false)")
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry: {e}", exc_info=True)

    # 2. Setup logging (may call basicConfig)
    # Note: logging is already configured at module level

    # 3. Attach OTel logging handler LAST (after logging is configured)
    try:
        if constants.OTEL_ENABLED and constants.OTEL_EXPORTER_OTLP_ENDPOINT:
            logger.info("Attaching OpenTelemetry logging handler...")
            success = otel_init.attach_logging_handler_simple()
            if success:
                logger.info(
                    "✅ OpenTelemetry logging handler attached - logs will be exported to Grafana"
                )
            else:
                logger.error(
                    "❌ Failed to attach OpenTelemetry logging handler - logs will NOT be exported to Grafana"
                )
        else:
            if not constants.OTEL_ENABLED:
                logger.warning(
                    "OpenTelemetry logging handler NOT attached - OTEL_ENABLED is false"
                )
            if not constants.OTEL_EXPORTER_OTLP_ENDPOINT:
                logger.error(
                    "❌ OpenTelemetry logging handler NOT attached - OTEL_EXPORTER_OTLP_ENDPOINT is empty! "
                    "Logs will NOT be exported to Grafana."
                )
    except Exception as e:
        logger.error(
            f"Failed to attach OpenTelemetry logging handler: {e}", exc_info=True
        )

    app = DataManagerApp()

    # Register signal handlers
    signal.signal(signal.SIGINT, app.shutdown)
    signal.signal(signal.SIGTERM, app.shutdown)

    try:
        await app.start()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Application error: {e}", exc_info=True)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
