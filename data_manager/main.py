"""
Main application entry point for Petrosa Data Manager.
"""

import asyncio
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING

# 1. Setup OpenTelemetry FIRST (before any other imports that might use it)
try:
    from petrosa_otel import (
        attach_logging_handler,
        setup_telemetry,
    )
except ImportError:
    setup_telemetry = None
    attach_logging_handler = None

import structlog
import uvicorn
from prometheus_client import start_http_server

import constants
from data_manager.api.app import create_app
from data_manager.consumer.decision_consumer import DecisionConsumer
from data_manager.consumer.execution_events_consumer import ExecutionEventsConsumer
from data_manager.consumer.intent_consumer import IntentConsumer
from data_manager.consumer.market_data_consumer import MarketDataConsumer
from data_manager.consumer.pnl_consumer import PnlConsumer
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
        self.intent_consumer: IntentConsumer | None = None
        self.decision_consumer: DecisionConsumer | None = None
        self.execution_events_consumer: ExecutionEventsConsumer | None = None
        self.pnl_consumer: PnlConsumer | None = None
        self.api_server_task: asyncio.Task | None = None
        self.leader_election: LeaderElectionManager | None = None
        self.backfill_orchestrator: BackfillOrchestrator | None = None
        self.ingest_evaluator = None  # P2.2 (#593) — set in start()
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

        # Start FastAPI server in background EARLY to handle health checks
        if constants.ENABLE_API:
            self.api_server_task = asyncio.create_task(self._run_api_server())
            logger.info("API server task created (health checks available)")

        # Initialize database connections
        try:
            self.db_manager = DatabaseManager()
            await self.db_manager.initialize()
            logger.info("Database connections initialized successfully")

            # Update API server with initialized db_manager
            if self.api_server_task:
                from data_manager import api

                api.app.db_manager = self.db_manager
                # Also update repositories in routers
                from data_manager.api.routes import config

                config.set_database_manager(self.db_manager)

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

        # Initialize and start NATS consumer.
        # Per #593 (P2.2): wire the consumer to the ingest evaluator
        # before starting it so the very first message is recorded. The
        # publisher uses the raw nats.aio.client.Client we hand it; for
        # the first start the consumer has not yet connected, so we
        # construct the publisher with a deferred lookup via a tiny
        # adapter — the evaluator only tries to publish on tick(), which
        # runs after the consumer is connected.
        if constants.ENABLE_API or True:  # Always start consumer for now
            from petrosa_otel.evaluators import NatsVerdictPublisher

            from data_manager.auditor.ingest_evaluator import IngestEvaluator

            class _DeferredNatsClient:
                """Forwards publish() to consumer.nats_client.nc after start."""

                def __init__(self, get_nc):
                    self._get_nc = get_nc

                async def publish(self, subject, payload):
                    nc = self._get_nc()
                    if nc is None:
                        return
                    await nc.publish(subject, payload)

            self.ingest_evaluator = IngestEvaluator(
                subjects=[constants.NATS_CONSUMER_SUBJECT],
                publisher=NatsVerdictPublisher(
                    nats_client=_DeferredNatsClient(
                        lambda: getattr(self.consumer.nats_client, "nc", None)
                        if self.consumer is not None
                        else None
                    )
                ),
            )
            self.consumer = MarketDataConsumer(
                db_manager=self.db_manager,
                ingest_evaluator=self.ingest_evaluator,
            )
            if await self.consumer.start():
                logger.info("Market data consumer started successfully")
                # Start the periodic ingest-evaluator tick loop.
                asyncio.create_task(self._run_ingest_evaluator_loop())
            else:
                logger.error("Failed to start market data consumer")

        # Initialize and start intent consumer (cross-service identifier contract, P0.2a)
        if constants.ENABLE_INTENT_CONSUMER:
            self.intent_consumer = IntentConsumer(db_manager=self.db_manager)
            if await self.intent_consumer.start():
                logger.info("Intent consumer started successfully")
            else:
                logger.error("Failed to start intent consumer")

        # Initialize and start CIO decision consumer (P0.2b)
        if constants.ENABLE_DECISION_CONSUMER:
            self.decision_consumer = DecisionConsumer(db_manager=self.db_manager)
            if await self.decision_consumer.start():
                logger.info("Decision consumer started successfully")
            else:
                logger.error("Failed to start decision consumer")

        # Initialize and start execution events consumer (P0.2c)
        if constants.ENABLE_EXECUTION_EVENTS_CONSUMER:
            self.execution_events_consumer = ExecutionEventsConsumer(
                db_manager=self.db_manager
            )
            if await self.execution_events_consumer.start():
                logger.info("Execution events consumer started successfully")
            else:
                logger.error("Failed to start execution events consumer")

        # Initialize and start pnl events consumer (P0.2d). Subscriber-only
        # today; the publisher side ships with P4.1 P&L computation. The
        # subscription is harmless until traffic arrives — at which point
        # the collection + indexes already exist.
        if constants.ENABLE_PNL_CONSUMER:
            self.pnl_consumer = PnlConsumer(db_manager=self.db_manager)
            if await self.pnl_consumer.start():
                logger.info("Pnl consumer started successfully")
            else:
                logger.error("Failed to start pnl consumer")

        # Initialize backfill orchestrator
        if self.db_manager and constants.ENABLE_BACKFILLER:
            from data_manager.backfiller.orchestrator import BackfillOrchestrator

            self.backfill_orchestrator = BackfillOrchestrator(self.db_manager)
            logger.info("Backfill orchestrator initialized")
        else:
            self.backfill_orchestrator = None

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

        # Flush telemetry first
        try:
            from petrosa_otel import flush_telemetry

            flush_telemetry()
            logger.info("✅ Telemetry flushed")
        except ImportError:
            pass

        # Stop leader election
        if self.leader_election:
            await self.leader_election.stop()
            logger.info("Leader election stopped")

        # Stop consumer
        if self.consumer:
            await self.consumer.stop()
            logger.info("Consumer stopped")

        # Stop intent consumer
        if self.intent_consumer:
            await self.intent_consumer.stop()
            logger.info("Intent consumer stopped")

        # Stop decision consumer
        if self.decision_consumer:
            await self.decision_consumer.stop()
            logger.info("Decision consumer stopped")

        # Stop execution events consumer
        if self.execution_events_consumer:
            await self.execution_events_consumer.stop()
            logger.info("Execution events consumer stopped")

        # Stop pnl consumer (P0.2d)
        if self.pnl_consumer:
            await self.pnl_consumer.stop()
            logger.info("Pnl consumer stopped")

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

        # Initialize and set configuration rate limiter
        try:
            from petrosa_otel import ConfigRateLimiter

            if ConfigRateLimiter:
                mongodb_client = (
                    self.db_manager.mongodb_adapter
                    if self.db_manager
                    and getattr(self.db_manager, "mongodb_adapter", None)
                    else None
                )
                if mongodb_client is not None:
                    rate_limiter = ConfigRateLimiter(
                        mongodb_client=mongodb_client,
                        service_name="data-manager",
                        per_agent_limit=int(
                            os.getenv("CONFIG_RATE_LIMIT_PER_AGENT", "10")
                        ),
                        cooldown_seconds=int(
                            os.getenv("CONFIG_RATE_LIMIT_COOLDOWN", "300")
                        ),
                    )
                    app.state.rate_limiter = rate_limiter
                    logger.info("✅ Configuration rate limiter initialized")
                else:
                    logger.warning(
                        "Database manager or MongoDB adapter not available; "
                        "skipping configuration rate limiter initialization."
                    )
        except ImportError:
            logger.warning(
                "petrosa_otel not found, skipping rate limiter initialization."
            )

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

        # Per #634: thread the underlying nats-py client into the
        # scheduler so the P2.1 evaluator framework can publish
        # `evaluator.data-manager.verdict` after each audit cycle.
        # `self.consumer.nats_client.nc` is the raw nats.aio.client.Client
        # the framework's `NatsVerdictPublisher` expects. Fall back to
        # ``None`` when the consumer hasn't connected (e.g. local dev
        # without a broker) — the scheduler then runs without publishing,
        # which is safe and was the legacy behavior.
        evaluator_nats_client = None
        if self.consumer is not None and getattr(self.consumer, "nats_client", None):
            evaluator_nats_client = getattr(self.consumer.nats_client, "nc", None)

        try:
            audit_scheduler = AuditScheduler(
                self.db_manager,
                leader_election=self.leader_election,
                backfill_orchestrator=self.backfill_orchestrator,
                nats_client=evaluator_nats_client,
            )
            await audit_scheduler.start()
        except Exception as e:
            logger.error(f"Error in auditor: {e}", exc_info=True)

        logger.info("Auditor stopped")

    async def _run_ingest_evaluator_loop(self) -> None:
        """Periodic tick loop for the P2.2 ingest evaluator (#593).

        Runs the evaluator at roughly half the silence threshold so
        the silence detector commits its post-hysteresis verdict
        within the NFR-R1 detection-time window.
        """
        from data_manager.auditor.ingest_evaluator import (
            DEFAULT_SILENCE_THRESHOLD_S,
        )

        interval = max(
            5,
            int(
                getattr(
                    constants,
                    "INGEST_EVALUATOR_TICK_INTERVAL",
                    DEFAULT_SILENCE_THRESHOLD_S // 2,
                )
            ),
        )
        logger.info(f"Ingest evaluator loop started (interval={interval}s)")
        while self.running:
            try:
                await asyncio.sleep(interval)
                if self.ingest_evaluator is not None:
                    await self.ingest_evaluator.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Ingest evaluator tick failed: {exc}")
        logger.info("Ingest evaluator loop stopped")

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
    # 1. Setup OpenTelemetry
    if constants.OTEL_ENABLED and setup_telemetry:
        try:
            logger.info("Initializing OpenTelemetry")
            setup_telemetry(
                service_name=constants.OTEL_SERVICE_NAME,
                service_type="async",
                enable_mysql=True,
                enable_mongodb=True,
                enable_http=True,
            )
        except Exception as e:
            logger.warning(
                f"petrosa_otel package not available or failed to initialize: {e}"
            )

    # 3. Attach OTel logging handler LAST (after logging is configured)
    if constants.OTEL_ENABLED and attach_logging_handler:
        try:
            success = attach_logging_handler()
            if success:
                logger.info(
                    "✅ OpenTelemetry logging handler attached - logs will be exported to Grafana"
                )
            else:
                logger.error(
                    "❌ Failed to attach OpenTelemetry logging handler - logs will NOT be exported to Grafana"
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
