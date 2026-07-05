"""
Main application entry point for Petrosa Data Manager.
"""

import asyncio
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING, Any

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
from data_manager.services.alert_dispatcher import AlertDispatcher

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
        self.execution_evaluator = None  # P2.4 (#595) — set in start()
        self.audit_evaluator = None  # P2.5 (#596) — set in start()
        self.pnl_publisher = None  # P4.1 follow-up (#652) — set in start()
        self.alert_dispatcher: AlertDispatcher | None = None  # #183 alert spine
        self.running = False
        self._shutdown_event = asyncio.Event()

    def _wire_route_publishers(self, deferred_publisher: Any) -> None:
        """Wire NATS publishers used by operator routes (#197).

        Both ``envelopes.changed`` (from envelopes.py route) and
        ``cio.config.leverage_bounds.updated`` (from leverage_bounds.py
        route) receive the SAME publisher instance (AC3 — one connection,
        two subjects). Extracted as a method so the wiring is testable in
        isolation without booting the full DataManagerApp (codecov gap fix
        for #197).
        """
        from data_manager.api.routes.envelopes import (
            set_envelopes_changed_publisher,
        )
        from data_manager.api.routes.leverage_bounds import (
            set_leverage_bounds_publisher,
        )

        self._envelope_leverage_publisher = deferred_publisher
        set_envelopes_changed_publisher(deferred_publisher)
        set_leverage_bounds_publisher(deferred_publisher)
        logger.info("Envelope/leverage NATS publishers wired at boot (#197)")

    def _unwire_route_publishers(self) -> None:
        """Unwire operator-route NATS publishers (#197 AC5).

        Passes ``None`` to both setters so the route modules' module-level
        ``_publisher`` globals reset — a subsequent restart starts from a
        clean slate, no stale closed-conn reference. Extracted from stop()
        for testability (codecov gap fix for #197).
        """
        try:
            from data_manager.api.routes.envelopes import (
                set_envelopes_changed_publisher,
            )
            from data_manager.api.routes.leverage_bounds import (
                set_leverage_bounds_publisher,
            )

            set_envelopes_changed_publisher(None)
            set_leverage_bounds_publisher(None)
            logger.info("Envelope/leverage NATS publishers unwired (#197)")
        except Exception as e:
            logger.warning(f"Failed to unwire route publishers: {e}")

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

            # Wire NATS publishers for operator endpoint cache-bust events (#197).
            # Both routes share ONE publisher instance (AC3): one connection, two
            # subjects (``envelopes.changed`` from envelopes.py + #193, and
            # ``cio.config.leverage_bounds.updated`` from leverage_bounds.py + #182).
            # The _DeferredNatsClient lambda defers nats_client lookup until publish
            # time, so wiring here — after consumer.start() succeeded — guarantees the
            # first accept/reject request resolves to a live NATS connection (AC4).
            self._wire_route_publishers(
                _DeferredNatsClient(
                    lambda: getattr(self.consumer.nats_client, "nc", None)
                    if self.consumer is not None
                    else None
                )
            )

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

        # Initialize and start alert dispatcher (#183, FR66). Subscribes to
        # `alerts.>`, persists every event into the `alerts` Mongo collection,
        # attempts delivery to the operator webhook (or marks delivered_mock
        # when no webhook is configured), and enforces per-category rate
        # limiting + summary rollup.
        if constants.ENABLE_ALERT_DISPATCHER:
            self.alert_dispatcher = AlertDispatcher(
                db_manager=self.db_manager,
                subject=constants.NATS_ALERTS_SUBJECT,
            )
            if await self.alert_dispatcher.start():
                logger.info("Alert dispatcher started successfully")
            else:
                logger.error("Failed to start alert dispatcher")

        # Initialize and start execution events consumer (P0.2c)
        if constants.ENABLE_EXECUTION_EVENTS_CONSUMER:
            # P4.1 follow-up (#652): bind a NATS-backed P&L publisher to the
            # consumer's `on_persisted` hook so every persisted fill emits a
            # `pnl.events.<strategy_id>` message. The publisher owns a long-
            # lived `PnlCalculator` so FIFO lot state survives between fills.
            on_persisted_hook = None
            if constants.ENABLE_PNL_PUBLISHER:
                from data_manager.services.pnl_publisher import PnlEventPublisher

                self.pnl_publisher = PnlEventPublisher(
                    nats_client=_DeferredNatsClient(
                        lambda: getattr(self.consumer.nats_client, "nc", None)
                        if self.consumer is not None
                        else None
                    ),
                )
                # Cold-start seed: replay the last N days of execution_events
                # so the FIFO lot state reflects positions opened before this
                # process started. Absence of mongo (limited mode) is fine —
                # the calculator just starts empty and the first live fill
                # opens lots from zero.
                await self._seed_pnl_calculator(self.pnl_publisher)
                on_persisted_hook = self.pnl_publisher.on_persisted

            self.execution_events_consumer = ExecutionEventsConsumer(
                db_manager=self.db_manager,
                on_persisted=on_persisted_hook,
            )
            if await self.execution_events_consumer.start():
                logger.info("Execution events consumer started successfully")
            else:
                logger.error("Failed to start execution events consumer")

        # Initialize and start execution evaluator (P2.4, #595). Runs over
        # the `execution_events` collection persisted by the consumer above.
        # The consumer is the precondition: without P0.2c traffic, the
        # evaluator's provider returns no rows and it stays in "unknown".
        if constants.ENABLE_EXECUTION_EVALUATOR and self.db_manager is not None:
            from petrosa_otel.evaluators import NatsVerdictPublisher

            from data_manager.auditor.execution_evaluator import (
                ExecutionEvaluator,
            )

            mongodb_adapter = self.db_manager.mongodb_adapter

            async def _execution_event_provider(start, end):
                # Pulled via closure so the evaluator never needs to know
                # about the database manager directly.
                return await mongodb_adapter.query_range(
                    "execution_events", start=start, end=end
                )

            self.execution_evaluator = ExecutionEvaluator(
                event_provider=_execution_event_provider,
                publisher=NatsVerdictPublisher(
                    nats_client=_DeferredNatsClient(
                        lambda: getattr(self.consumer.nats_client, "nc", None)
                        if self.consumer is not None
                        else None
                    )
                ),
            )
            asyncio.create_task(self._run_execution_evaluator_loop())
            logger.info("Execution evaluator started successfully")

        # Initialize and start audit evaluator (P2.5, #596). Runs over the
        # `intents`, `cio_decisions`, and `execution_events` collections
        # plus the Prometheus receipt/persist counters on each consumer.
        if constants.ENABLE_AUDIT_EVALUATOR and self.db_manager is not None:
            from petrosa_otel.evaluators import NatsVerdictPublisher

            from data_manager.auditor.audit_evaluator import AuditEvaluator
            from data_manager.consumer.decision_consumer import (
                decision_messages_persisted,
                decision_messages_received,
            )
            from data_manager.consumer.execution_events_consumer import (
                execution_messages_persisted,
                execution_messages_received,
            )
            from data_manager.consumer.intent_consumer import (
                intent_messages_persisted,
                intent_messages_received,
            )

            def _sum_metric(metric) -> int:
                """Sum every sample of a prometheus Counter, label or no label."""
                total = 0
                for family in metric.collect():
                    for sample in family.samples:
                        if sample.name.endswith("_total"):
                            total += int(sample.value)
                return total

            def _audit_counter_source() -> dict[str, tuple[int, int]]:
                return {
                    "intent": (
                        _sum_metric(intent_messages_received),
                        _sum_metric(intent_messages_persisted),
                    ),
                    "decision": (
                        _sum_metric(decision_messages_received),
                        _sum_metric(decision_messages_persisted),
                    ),
                    "execution": (
                        _sum_metric(execution_messages_received),
                        _sum_metric(execution_messages_persisted),
                    ),
                }

            audit_mongodb = self.db_manager.mongodb_adapter

            async def _audit_event_source(collection, start, end):
                return await audit_mongodb.query_range(collection, start=start, end=end)

            self.audit_evaluator = AuditEvaluator(
                counter_source=_audit_counter_source,
                event_source=_audit_event_source,
                lookback_s=constants.AUDIT_EVALUATOR_LOOKBACK_S,
                publisher=NatsVerdictPublisher(
                    nats_client=_DeferredNatsClient(
                        lambda: getattr(self.consumer.nats_client, "nc", None)
                        if self.consumer is not None
                        else None
                    )
                ),
            )
            asyncio.create_task(self._run_audit_evaluator_loop())
            logger.info("Audit evaluator started successfully")

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
        asyncio.create_task(self._run_drawdown_scheduler())

        self.running = True
        logger.info("All components started successfully")

        # Periodic MongoDB logical-data-size gauge refresh (dm#248). Created
        # AFTER self.running=True so the `while self.running` loop runs its
        # first iteration immediately (the series appears within one refresh
        # interval of boot) rather than exiting on a stale False.
        asyncio.create_task(self._run_mongo_data_size_loop())

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

        # Unwire operator-route NATS publishers (#197 AC5). Done BEFORE
        # consumer.stop() closes the underlying nats_client.nc so a late
        # in-flight accept/reject request gets `publisher is None` (graceful
        # no-op) instead of a publish() against a half-closed connection.
        self._unwire_route_publishers()

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

        # Stop alert dispatcher (#183)
        if self.alert_dispatcher:
            await self.alert_dispatcher.stop()
            logger.info("Alert dispatcher stopped")

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

    async def _run_execution_evaluator_loop(self) -> None:
        """Periodic tick loop for the P2.4 execution evaluator (#595).

        Cadence is half of the error-rate window by default so a
        committed-unhealthy verdict surfaces within roughly one
        detection-time budget after the underlying anomaly begins.
        """
        interval = max(
            5,
            int(
                getattr(
                    constants,
                    "EXECUTION_EVALUATOR_TICK_INTERVAL",
                    150,
                )
            ),
        )
        logger.info(f"Execution evaluator loop started (interval={interval}s)")
        while self.running:
            try:
                await asyncio.sleep(interval)
                if self.execution_evaluator is not None:
                    await self.execution_evaluator.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Execution evaluator tick failed: {exc}")
        logger.info("Execution evaluator loop stopped")

    async def _run_audit_evaluator_loop(self) -> None:
        """Periodic tick loop for the P2.5 audit evaluator (#596).

        Default cadence (5 min) gives the consume-without-persist detector
        a meaningful slice between snapshots; shorter cadences cause
        oscillation when persistence batches lag receipts by one tick.
        """
        interval = max(
            5,
            int(
                getattr(
                    constants,
                    "AUDIT_EVALUATOR_TICK_INTERVAL",
                    300,
                )
            ),
        )
        logger.info(f"Audit evaluator loop started (interval={interval}s)")
        while self.running:
            try:
                await asyncio.sleep(interval)
                if self.audit_evaluator is not None:
                    await self.audit_evaluator.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Audit evaluator tick failed: {exc}")
        logger.info("Audit evaluator loop stopped")

    async def _seed_pnl_calculator(self, publisher) -> None:
        """Replay historical execution_events to seed the P&L calculator (#652).

        Without seeding, the first fills after a restart would mis-state
        realized P&L because the FIFO lot state would be empty. We pull
        ``PNL_PUBLISHER_SEED_DAYS`` of `execution_events` and replay them
        through `publisher.calculator.apply_fill` without publishing. The
        replay is best-effort: missing mongo (limited mode) or a query
        failure both degrade to an empty calculator, which is the same
        behavior as a true cold start with no prior history.
        """
        from datetime import datetime, timedelta

        try:
            from datetime import UTC
        except ImportError:  # Python 3.10 compatibility
            from datetime import timezone

            UTC = timezone.utc  # noqa: UP017

        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.info(
                "pnl_publisher_seed_skipped",
                extra={"reason": "no_mongodb_adapter"},
            )
            return

        end = datetime.now(UTC)
        start = end - timedelta(days=constants.PNL_PUBLISHER_SEED_DAYS)
        try:
            rows = await self.db_manager.mongodb_adapter.query_range(
                "execution_events", start=start, end=end
            )
        except Exception as exc:
            logger.warning(
                "pnl_publisher_seed_failed",
                extra={"error": str(exc), "days": constants.PNL_PUBLISHER_SEED_DAYS},
            )
            return

        applied = publisher.replay_history(rows)
        logger.info(
            "pnl_publisher_seed_complete",
            extra={
                "rows_scanned": len(rows),
                "fills_applied": applied,
                "days": constants.PNL_PUBLISHER_SEED_DAYS,
            },
        )

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

    async def _run_drawdown_scheduler(self) -> None:
        """Periodic drawdown-vs-envelope check (P4.2, #602; FR30).

        Iterates known strategies on each tick, computes current
        drawdown against the latest characterization envelope, and
        emits a breach event on ``portfolio.drawdown.breach.{sid}``
        so CIO can intervene. Healthy ticks do not publish — only
        envelope breaches do.

        Surfaced as the FR30 production-wiring gap by the 2026-05-22
        reality-check audit (see prd-reality-check-2026-05-22.md).
        """
        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.warning("DrawdownScheduler not started: no mongodb_adapter")
            return

        if not self.db_manager.is_healthy():
            logger.warning("DrawdownScheduler not started: databases not healthy")
            return

        # Import here to avoid circular dependency at module load.
        from data_manager.db.repositories.characterization_repository import (
            CharacterizationRepository,
        )
        from data_manager.portfolio.drawdown_scheduler import (
            DrawdownScheduler,
        )

        # Same lazy-extraction pattern used by the audit scheduler:
        # the breach publisher tolerates a None client (logs + drops
        # without raising), so local-dev without a broker is safe.
        drawdown_nats_client = None
        if self.consumer is not None and getattr(self.consumer, "nats_client", None):
            drawdown_nats_client = getattr(self.consumer.nats_client, "nc", None)

        try:
            char_repo = CharacterizationRepository(
                mysql_adapter=None,
                mongodb_adapter=self.db_manager.mongodb_adapter,
            )
            scheduler = DrawdownScheduler(
                mongodb_adapter=self.db_manager.mongodb_adapter,
                characterization_repository=char_repo,
                nats_client=drawdown_nats_client,
            )
            await scheduler.start()
            logger.info(
                "drawdown_scheduler_started",
                extra={"interval_seconds": 300},
            )
        except Exception as e:
            logger.error(f"Error in DrawdownScheduler: {e}", exc_info=True)

    async def _run_mongo_data_size_loop(self) -> None:
        """Periodic MongoDB logical-data-size gauge refresh (dm#248).

        Producer half of the Atlas M0 data-size leading-indicator alert
        (petrosa_k8s#905 / dm#244 AC6). Refreshes
        the ``data_manager_mongo_data_size_bytes`` cache from ``dbStats().dataSize``
        every ``MONGO_DATA_SIZE_REFRESH_INTERVAL`` seconds; the OTLP
        ObservableGauge then exports it into Grafana Cloud (dm#252 — the
        prometheus_client :9090 registry is NOT scraped there).

        The refresh is failure-isolated inside
        :func:`refresh_mongo_data_size` (a Mongo error leaves the last-known
        value and bumps ``..._scrape_errors_total``); the extra try/except
        here is defense-in-depth so a genuinely unexpected error can never
        kill the loop.
        """
        if not constants.ENABLE_MONGO_DATA_SIZE_GAUGE:
            logger.info("Mongo data-size gauge disabled")
            return
        if not self.db_manager or not getattr(self.db_manager, "mongodb_adapter", None):
            logger.warning("Mongo data-size gauge loop not started: no mongodb_adapter")
            return

        from data_manager.maintenance.mongo_data_size_gauge import (
            refresh_mongo_data_size,
        )

        interval = max(
            10,
            int(getattr(constants, "MONGO_DATA_SIZE_REFRESH_INTERVAL", 60)),
        )
        adapter = self.db_manager.mongodb_adapter
        extra_dbs = tuple(getattr(constants, "MONGO_DATA_SIZE_DATABASES", ()))
        logger.info(f"Mongo data-size gauge loop started (interval={interval}s)")
        while self.running:
            try:
                await refresh_mongo_data_size(adapter, extra_databases=extra_dbs)
            except Exception as exc:
                logger.warning(f"Mongo data-size refresh failed: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
        logger.info("Mongo data-size gauge loop stopped")

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
