"""Execution evaluator — P2.4 (petrosa_k8s#595, FR21).

Detects anomalies in execution telemetry persisted to the ``execution_events``
audit-trail collection (P0.2c) and emits health verdicts on
``evaluator.execution.verdict`` via the shared :mod:`petrosa_otel.evaluators`
framework (P2.1, petrosa_k8s#592). Four detectors run on every tick:

  * **Slippage** — rolling per-symbol z-score of fill prices over the
    slippage window. A current fill that is >3σ from the trailing mean
    (default window 1 h) trips the detector. This is a price-stability
    proxy rather than a true ``realized vs. expected`` slippage; an
    "expected price" field is not present on the persisted event today,
    so the implementation flags abnormal fill-price excursions instead.
    The detector is silent until enough samples are collected
    (``MIN_SLIPPAGE_SAMPLES``).

  * **Fill rate** — per (strategy_id, symbol) the ratio of ``filled``
    events to ``placed`` events over the fill-rate window (default 1 h).
    A current fill rate below ``MIN_FILL_RATE`` after the window has
    accumulated at least ``MIN_FILL_RATE_PLACED_SAMPLES`` placed orders
    trips the detector.

  * **Risk-posture drift** — per strategy, the gross notional flow
    over the recent window (default 1 h, ``risk_window_s``) divided
    by the trailing 24 h gross notional median. When the multiple
    exceeds ``RISK_VELOCITY_MULTIPLE`` (default 2×) the detector trips.

  * **Exchange error rate** — share of ``rejected`` events over the
    error-rate window (default 5 min). Above ``REJECT_RATE_BUDGET``
    (default 5 %) the detector trips. Reasons starting with ``429`` /
    ``rate_limit`` are additionally tracked separately and trip at the
    tighter ``RATE_LIMIT_BUDGET`` (default 1 %).

The evaluator owns no I/O directly — on each :meth:`evaluate` invocation
it calls the injected ``event_provider`` (typically the MongoDB-backed
default) for the last ``lookback`` of events and computes the four
signals in one pass over the rows. Time is injected via ``time_source``
so the unit tests stay deterministic.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
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


SUBSYSTEM = "execution"

# Detection windows (seconds).
DEFAULT_SLIPPAGE_WINDOW_S = 60 * 60  # 1 h
DEFAULT_FILL_RATE_WINDOW_S = 60 * 60  # 1 h
DEFAULT_RISK_WINDOW_S = 60 * 60  # 1 h
DEFAULT_RISK_BASELINE_S = 24 * 60 * 60  # 24 h
DEFAULT_ERROR_WINDOW_S = 5 * 60  # 5 min

# Detector thresholds.
DEFAULT_SLIPPAGE_Z_THRESHOLD = 3.0
DEFAULT_MIN_FILL_RATE = 0.5
DEFAULT_RISK_VELOCITY_MULTIPLE = 2.0
DEFAULT_REJECT_RATE_BUDGET = 0.05
DEFAULT_RATE_LIMIT_BUDGET = 0.01

# Minimum samples before a detector is allowed to trip. Avoids
# unstable verdicts on cold start or low-volume markets.
MIN_SLIPPAGE_SAMPLES = 30
MIN_FILL_RATE_PLACED_SAMPLES = 10
MIN_RISK_BASELINE_SAMPLES = 10
MIN_ERROR_RATE_TOTAL = 20

# Reasons matching these case-insensitive substrings count as rate-limit
# rejections separately from the broader 4xx bucket.
RATE_LIMIT_REASON_MARKERS = ("429", "rate_limit", "rate-limit", "ratelimit")

# Type aliases.
EventProvider = Callable[[datetime, datetime], Awaitable[list[dict[str, Any]]]]


@dataclass
class _DetectorSignal:
    """Result of one detector. ``tripped`` drives the verdict."""

    tripped: bool
    reason: str | None = None  # populated when tripped


class ExecutionEvaluator(Evaluator):
    """Subsystem evaluator for trade execution anomalies (P2.4)."""

    def __init__(
        self,
        *,
        event_provider: EventProvider,
        publisher: VerdictPublisher | None = None,
        hysteresis: HysteresisPolicy | None = None,
        slippage_window_s: int = DEFAULT_SLIPPAGE_WINDOW_S,
        fill_rate_window_s: int = DEFAULT_FILL_RATE_WINDOW_S,
        risk_window_s: int = DEFAULT_RISK_WINDOW_S,
        risk_baseline_s: int = DEFAULT_RISK_BASELINE_S,
        error_window_s: int = DEFAULT_ERROR_WINDOW_S,
        slippage_z_threshold: float = DEFAULT_SLIPPAGE_Z_THRESHOLD,
        min_fill_rate: float = DEFAULT_MIN_FILL_RATE,
        risk_velocity_multiple: float = DEFAULT_RISK_VELOCITY_MULTIPLE,
        reject_rate_budget: float = DEFAULT_REJECT_RATE_BUDGET,
        rate_limit_budget: float = DEFAULT_RATE_LIMIT_BUDGET,
        time_source: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            subsystem=SUBSYSTEM,
            publisher=publisher,
            hysteresis=hysteresis,
        )
        self._event_provider = event_provider
        self._slippage = timedelta(seconds=slippage_window_s)
        self._fill_rate = timedelta(seconds=fill_rate_window_s)
        self._risk = timedelta(seconds=risk_window_s)
        self._risk_baseline = timedelta(seconds=risk_baseline_s)
        self._error = timedelta(seconds=error_window_s)
        self._slippage_z = slippage_z_threshold
        self._min_fill_rate = min_fill_rate
        self._risk_multiple = risk_velocity_multiple
        self._reject_budget = reject_rate_budget
        self._rate_limit_budget = rate_limit_budget
        self._time = time_source or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Framework hook.
    # ------------------------------------------------------------------

    async def evaluate(self) -> tuple[str, str]:
        """Compute the current (verdict, reason) sample.

        Pulls the widest lookback window we need from the provider once
        (max of the baseline windows) so all four detectors share a
        single read. Returns the highest-priority unhealthy signal if
        any; otherwise ``healthy``.
        """
        now = self._time()
        lookback = max(
            self._slippage, self._fill_rate, self._risk_baseline, self._error
        )
        try:
            events = await self._event_provider(now - lookback, now)
        except Exception as exc:  # pragma: no cover — defensive
            return "unknown", f"event provider error: {type(exc).__name__}"

        if not events:
            return "unknown", "no execution events in lookback window"

        # Detectors run in priority order — rate-limit and rejection
        # rates first because they degrade trading immediately, then
        # fill rate (placement reliability), then risk drift, then
        # slippage (statistical, slowest).
        for signal in (
            self._exchange_error_rate(events, now),
            self._fill_rate_signal(events, now),
            self._risk_posture_drift(events, now),
            self._slippage_signal(events, now),
        ):
            if signal.tripped:
                return "unhealthy", signal.reason or "execution anomaly"

        return "healthy", "no slippage, fill-rate, risk, or error anomalies"

    # ------------------------------------------------------------------
    # Detectors. Each consumes the shared event list once and returns
    # a _DetectorSignal. Detectors are tolerant: missing fields cause
    # the row to be skipped, not the whole tick to abort.
    # ------------------------------------------------------------------

    def _exchange_error_rate(
        self, events: list[dict[str, Any]], now: datetime
    ) -> _DetectorSignal:
        cutoff = now - self._error
        recent = [e for e in events if _event_ts(e) >= cutoff]
        total = len(recent)
        if total < MIN_ERROR_RATE_TOTAL:
            return _DetectorSignal(False)

        rejected = [e for e in recent if e.get("event_type") == "rejected"]
        reject_rate = len(rejected) / total
        rate_limited = [
            e
            for e in rejected
            if _reason_matches(e.get("reason"), RATE_LIMIT_REASON_MARKERS)
        ]
        rate_limit_rate = len(rate_limited) / total

        if rate_limit_rate > self._rate_limit_budget:
            return _DetectorSignal(
                True,
                f"rate-limit rejections {rate_limit_rate:.2%} of {total} events "
                f"in {int(self._error.total_seconds())}s "
                f"(budget {self._rate_limit_budget:.2%})",
            )
        if reject_rate > self._reject_budget:
            return _DetectorSignal(
                True,
                f"rejected {reject_rate:.2%} of {total} events in "
                f"{int(self._error.total_seconds())}s "
                f"(budget {self._reject_budget:.2%})",
            )
        return _DetectorSignal(False)

    def _fill_rate_signal(
        self, events: list[dict[str, Any]], now: datetime
    ) -> _DetectorSignal:
        cutoff = now - self._fill_rate
        placed: dict[tuple[str, str], int] = defaultdict(int)
        filled: dict[tuple[str, str], int] = defaultdict(int)
        for e in events:
            if _event_ts(e) < cutoff:
                continue
            sid = e.get("strategy_id")
            sym = e.get("symbol")
            if not sid or not sym:
                continue
            key = (sid, sym)
            etype = e.get("event_type")
            if etype == "placed":
                placed[key] += 1
            elif etype in {"filled", "partial_fill"}:
                filled[key] += 1

        worst_key: tuple[str, str] | None = None
        worst_rate = 1.0
        worst_placed = 0
        for key, p in placed.items():
            if p < MIN_FILL_RATE_PLACED_SAMPLES:
                continue
            rate = filled.get(key, 0) / p
            if rate < worst_rate:
                worst_rate = rate
                worst_key = key
                worst_placed = p

        if worst_key is not None and worst_rate < self._min_fill_rate:
            sid, sym = worst_key
            return _DetectorSignal(
                True,
                f"fill rate {worst_rate:.0%} for {sid}/{sym} over "
                f"{worst_placed} placed in {int(self._fill_rate.total_seconds())}s "
                f"(min {self._min_fill_rate:.0%})",
            )
        return _DetectorSignal(False)

    def _risk_posture_drift(
        self, events: list[dict[str, Any]], now: datetime
    ) -> _DetectorSignal:
        recent_cut = now - self._risk
        baseline_cut = now - self._risk_baseline
        # Per-strategy gross notional (qty * price) for filled events.
        recent: dict[str, float] = defaultdict(float)
        baseline_buckets: dict[str, list[float]] = defaultdict(list)

        # Bucket size for the trailing median = recent window, so we
        # compare apples to apples (one "recent" bucket vs many baseline
        # buckets of the same length).
        bucket_size = self._risk
        num_buckets = max(1, int(self._risk_baseline / bucket_size))

        for e in events:
            ts = _event_ts(e)
            if ts < baseline_cut:
                continue
            if e.get("event_type") not in {"filled", "partial_fill"}:
                continue
            sid = e.get("strategy_id")
            if not sid:
                continue
            qty = e.get("fill_qty") or e.get("qty")
            price = e.get("price")
            if qty is None or price is None:
                continue
            notional = abs(float(qty) * float(price))

            if ts >= recent_cut:
                recent[sid] += notional
            else:
                # Which historical bucket does this event belong to?
                offset = int((now - ts) / bucket_size)
                if 1 <= offset <= num_buckets:
                    # Pad the per-strategy bucket list so the median has
                    # entries for all baseline buckets (even zero-flow
                    # ones — those count toward the median).
                    if len(baseline_buckets[sid]) < num_buckets:
                        baseline_buckets[sid].extend(
                            [0.0] * (num_buckets - len(baseline_buckets[sid]))
                        )
                    baseline_buckets[sid][offset - 1] += notional

        for sid, recent_flow in recent.items():
            buckets = baseline_buckets.get(sid, [])
            non_zero = [b for b in buckets if b > 0]
            if len(non_zero) < MIN_RISK_BASELINE_SAMPLES:
                continue
            median_flow = statistics.median(non_zero)
            if median_flow == 0:
                continue
            multiple = recent_flow / median_flow
            if multiple > self._risk_multiple:
                return _DetectorSignal(
                    True,
                    f"risk velocity {multiple:.1f}x median for {sid} over "
                    f"{int(self._risk.total_seconds())}s "
                    f"(threshold {self._risk_multiple:.1f}x)",
                )
        return _DetectorSignal(False)

    def _slippage_signal(
        self, events: list[dict[str, Any]], now: datetime
    ) -> _DetectorSignal:
        cutoff = now - self._slippage
        # Per-symbol fill prices in the slippage window.
        prices: dict[str, list[float]] = defaultdict(list)
        latest_per_symbol: dict[str, tuple[datetime, float]] = {}
        for e in events:
            ts = _event_ts(e)
            if ts < cutoff:
                continue
            if e.get("event_type") not in {"filled", "partial_fill"}:
                continue
            sym = e.get("symbol")
            price = e.get("price")
            if not sym or price is None:
                continue
            p = float(price)
            prices[sym].append(p)
            cur = latest_per_symbol.get(sym)
            if cur is None or ts > cur[0]:
                latest_per_symbol[sym] = (ts, p)

        for sym, samples in prices.items():
            if len(samples) < MIN_SLIPPAGE_SAMPLES:
                continue
            mean = statistics.fmean(samples)
            stdev = statistics.pstdev(samples)
            if stdev == 0:
                continue
            _, latest_price = latest_per_symbol[sym]
            z = abs(latest_price - mean) / stdev
            if z > self._slippage_z:
                return _DetectorSignal(
                    True,
                    f"fill-price z={z:.1f} for {sym} over {len(samples)} fills "
                    f"in {int(self._slippage.total_seconds())}s "
                    f"(threshold {self._slippage_z:.1f}σ)",
                )
        return _DetectorSignal(False)


# ----------------------------------------------------------------------
# Helpers.
# ----------------------------------------------------------------------


def _event_ts(e: dict[str, Any]) -> datetime:
    """Pull the event timestamp, normalising naive datetimes to UTC."""
    ts = e.get("timestamp")
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    # Unparseable rows sort to the epoch so they're outside every window.
    return datetime.fromtimestamp(0, tz=UTC)


def _reason_matches(reason: Any, markers: tuple[str, ...]) -> bool:
    if not reason:
        return False
    lowered = str(reason).lower()
    return any(m in lowered for m in markers)
