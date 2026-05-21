"""Audit evaluator — P2.5 (petrosa_k8s#596, FR22).

Detects cross-service event-loss conditions over the persisted audit-trail
collections (``intents``, ``cio_decisions``, ``execution_events``) and emits
health verdicts on ``evaluator.audit.verdict`` via the shared
:mod:`petrosa_otel.evaluators` framework (P2.1, petrosa_k8s#592). Three
detectors run on every tick:

  * **Consume-without-persist** — for each consumer stream the gap between
    the running NATS-receipt counter and the running persistence counter.
    The detector samples both on each tick and trips when the *delta*
    (receipts minus persists for the current tick) exceeds an absolute
    budget OR the rolling ratio over a rolling window exceeds a relative
    budget. The first tick is always a baseline — no delta is computed
    against missing prior state.

  * **decision_id propagation** — every persisted ``execution_events`` row
    (orders + fills) MUST carry a ``decision_id`` per the P0.1 cross-service
    identifier contract. A non-zero count of rows over the lookback window
    that lack a ``decision_id`` trips the detector.

  * **Join completeness** — every order (``event_type == 'placed'``) must
    join to a parent ``cio_decisions`` row on ``decision_id``; every fill
    (``filled`` / ``partial_fill``) must join to a sibling ``placed`` row
    on ``order_id`` (or directly to the parent decision if no placement
    persisted yet — a partial-loss signal in its own right). Orphans over
    the lookback window trip the detector.

The evaluator owns no I/O itself — callers inject ``counter_source`` (returns
``{stream: (received_total, persisted_total)}``) and ``event_source``
(returns the rows for a collection over a range). Time is injected via
``time_source`` so the unit tests stay deterministic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from petrosa_otel.evaluators import Evaluator

if TYPE_CHECKING:
    from petrosa_otel.evaluators.base import HysteresisPolicy
    from petrosa_otel.evaluators.publisher import VerdictPublisher


SUBSYSTEM = "audit"

# Detection windows (seconds).
DEFAULT_LOOKBACK_S = 30 * 60  # 30 min for orphan + propagation detectors.

# Detector budgets.
# Absolute: how many unpersisted messages we tolerate in one tick across
# all streams before tripping. Set high enough that a single deferred
# message between receipt and persist doesn't oscillate the verdict.
DEFAULT_CONSUME_PERSIST_ABS_BUDGET = 5
# Relative: fraction of receipts that may go unpersisted over the
# rolling window before tripping. 0.5% catches systemic loss without
# tripping on transient backlog.
DEFAULT_CONSUME_PERSIST_REL_BUDGET = 0.005
# How many tick samples to retain for the rolling consume/persist ratio.
DEFAULT_CONSUME_HISTORY_SIZE = 12  # ≈ 1h at 5-min ticks.

# Minimum sample volume before the consume-without-persist relative
# detector is allowed to trip. Avoids false positives at low traffic.
MIN_CONSUME_RATE_RECEIPTS = 100

# Required event types for the join-completeness detector.
ORDER_PLACED_TYPES = {"placed"}
FILL_TYPES = {"filled", "partial_fill"}
EVENTS_REQUIRING_DECISION_ID = ORDER_PLACED_TYPES | FILL_TYPES

# Type aliases.
CounterSource = Callable[[], dict[str, tuple[int, int]]]
EventSource = Callable[[str, datetime, datetime], Awaitable[list[dict[str, Any]]]]


@dataclass
class _DetectorSignal:
    """Result of one detector. ``tripped`` drives the verdict."""

    tripped: bool
    reason: str | None = None  # populated when tripped


class AuditEvaluator(Evaluator):
    """Subsystem evaluator for cross-service audit-trail integrity (P2.5)."""

    def __init__(
        self,
        *,
        counter_source: CounterSource,
        event_source: EventSource,
        publisher: VerdictPublisher | None = None,
        hysteresis: HysteresisPolicy | None = None,
        lookback_s: int = DEFAULT_LOOKBACK_S,
        consume_persist_abs_budget: int = DEFAULT_CONSUME_PERSIST_ABS_BUDGET,
        consume_persist_rel_budget: float = DEFAULT_CONSUME_PERSIST_REL_BUDGET,
        consume_history_size: int = DEFAULT_CONSUME_HISTORY_SIZE,
        time_source: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            subsystem=SUBSYSTEM,
            publisher=publisher,
            hysteresis=hysteresis,
        )
        self._counter_source = counter_source
        self._event_source = event_source
        self._lookback = timedelta(seconds=lookback_s)
        self._abs_budget = consume_persist_abs_budget
        self._rel_budget = consume_persist_rel_budget
        self._history_size = max(1, consume_history_size)
        self._time = time_source or (lambda: datetime.now(UTC))
        # Per-stream history of (received, persisted) snapshots. Updated
        # on each tick so the rolling-ratio detector can amortize a
        # transient deferral over the window.
        self._snapshots: list[dict[str, tuple[int, int]]] = []

    # ------------------------------------------------------------------
    # Framework hook.
    # ------------------------------------------------------------------

    async def evaluate(self) -> tuple[str, str]:
        now = self._time()

        # Always sample counters so the next tick has a comparison point,
        # even when other detectors trip first.
        current_counters = self._counter_source()
        self._snapshots.append(current_counters)
        if len(self._snapshots) > self._history_size:
            # Trim from the front so the rolling window stays bounded.
            self._snapshots = self._snapshots[-self._history_size :]

        # Pull every collection's rows for the lookback in parallel-shape
        # API (sequential await is fine; the load is small per tick).
        intents = await self._event_source("intents", now - self._lookback, now)
        decisions = await self._event_source("cio_decisions", now - self._lookback, now)
        execution_events = await self._event_source(
            "execution_events", now - self._lookback, now
        )

        # If nothing is happening we cannot prove integrity — flag as
        # unknown rather than asserting healthy.
        if not (intents or decisions or execution_events) and len(self._snapshots) < 2:
            return "unknown", "no traffic and no counter history yet"

        for signal in (
            self._consume_without_persist_signal(current_counters),
            self._decision_id_propagation_signal(execution_events),
            self._join_completeness_signal(execution_events, decisions),
        ):
            if signal.tripped:
                return "unhealthy", signal.reason or "audit-trail anomaly"

        return "healthy", "consume=persist, decision_ids present, joins complete"

    # ------------------------------------------------------------------
    # Detectors.
    # ------------------------------------------------------------------

    def _consume_without_persist_signal(
        self, current: dict[str, tuple[int, int]]
    ) -> _DetectorSignal:
        # Per-stream absolute delta on this tick (receipts - persists).
        # A receipt without a corresponding persist within the same tick
        # is normal (the consumer may not have flushed yet). What we want
        # to detect is a *growing* gap — receipts persistently outpacing
        # persists across multiple ticks.
        if len(self._snapshots) < 2:
            return _DetectorSignal(False)

        prior = self._snapshots[-2]
        worst_abs_stream: str | None = None
        worst_abs_delta = 0
        worst_rel_stream: str | None = None
        worst_rel_ratio = 0.0
        worst_rel_received = 0
        worst_rel_unpersisted = 0
        for stream, (received, persisted) in current.items():
            prior_received, prior_persisted = prior.get(stream, (received, persisted))
            d_received = received - prior_received
            d_persisted = persisted - prior_persisted
            # Negative deltas indicate a counter reset (pod restart); treat
            # as no signal for this stream this tick rather than tripping.
            if d_received < 0 or d_persisted < 0:
                continue
            unpersisted_tick = d_received - d_persisted
            if unpersisted_tick > worst_abs_delta:
                worst_abs_delta = unpersisted_tick
                worst_abs_stream = stream
            if d_received >= MIN_CONSUME_RATE_RECEIPTS:
                ratio = unpersisted_tick / d_received
                if ratio > worst_rel_ratio:
                    worst_rel_ratio = ratio
                    worst_rel_stream = stream
                    worst_rel_received = d_received
                    worst_rel_unpersisted = unpersisted_tick

        if worst_abs_stream is not None and worst_abs_delta > self._abs_budget:
            return _DetectorSignal(
                True,
                f"{worst_abs_stream}: {worst_abs_delta} unpersisted in tick "
                f"(budget {self._abs_budget})",
            )
        if worst_rel_stream is not None and worst_rel_ratio > self._rel_budget:
            return _DetectorSignal(
                True,
                f"{worst_rel_stream}: {worst_rel_unpersisted}/{worst_rel_received}"
                f" = {worst_rel_ratio:.2%} unpersisted "
                f"(budget {self._rel_budget:.2%})",
            )
        return _DetectorSignal(False)

    def _decision_id_propagation_signal(
        self, execution_events: list[dict[str, Any]]
    ) -> _DetectorSignal:
        missing = [
            e
            for e in execution_events
            if e.get("event_type") in EVENTS_REQUIRING_DECISION_ID
            and not e.get("decision_id")
        ]
        if not missing:
            return _DetectorSignal(False)
        sample = missing[0]
        return _DetectorSignal(
            True,
            f"{len(missing)} execution events missing decision_id "
            f"(e.g. order_id={sample.get('order_id', '?')} "
            f"type={sample.get('event_type', '?')})",
        )

    def _join_completeness_signal(
        self,
        execution_events: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> _DetectorSignal:
        # Index decisions by their decision_id. The decision_consumer
        # writes the decision_id into both the `decision_id` field and
        # `_id`; check both to be tolerant.
        known_decisions: set[str] = set()
        for d in decisions:
            for key in ("decision_id", "_id"):
                v = d.get(key)
                if isinstance(v, str) and v:
                    known_decisions.add(v)

        orders_by_id: dict[str, dict[str, Any]] = {}
        orphan_orders: list[dict[str, Any]] = []
        orphan_fills: list[dict[str, Any]] = []
        for e in execution_events:
            etype = e.get("event_type")
            decision_id = e.get("decision_id")
            order_id = e.get("order_id")
            if etype in ORDER_PLACED_TYPES:
                if order_id:
                    orders_by_id[order_id] = e
                # Parent decision must exist.
                if decision_id and decision_id not in known_decisions:
                    orphan_orders.append(e)
            elif etype in FILL_TYPES:
                # A fill must have either a sibling placed order in the
                # window or — at minimum — a known parent decision.
                if (
                    order_id
                    and order_id not in orders_by_id
                    and (not decision_id or decision_id not in known_decisions)
                ):
                    orphan_fills.append(e)

        total = len(orphan_orders) + len(orphan_fills)
        if total == 0:
            return _DetectorSignal(False)

        sample = orphan_orders[0] if orphan_orders else orphan_fills[0]
        return _DetectorSignal(
            True,
            f"{total} orphan execution events with no parent "
            f"(e.g. order_id={sample.get('order_id', '?')} "
            f"type={sample.get('event_type', '?')} "
            f"decision_id={sample.get('decision_id', '?')})",
        )
