"""Ingest evaluator — P2.2 (petrosa_k8s#593, FR19).

Monitors live NATS market-data subjects and emits health verdicts on
``evaluator.ingest.verdict`` via the shared :mod:`petrosa_otel.evaluators`
framework (P2.1, petrosa_k8s#592). Detects three failure modes:

  * **Silence** — no message on a subscribed subject for more than the
    configured threshold (default 60 s).
  * **Staleness** — the latest message payload's own timestamp is older
    than wall-clock by more than the configured threshold (default 30 s).
    Only checked when a payload timestamp is observed; absence is not
    treated as stale (consumers without a timestamp field just don't
    surface this signal).
  * **Integrity loss** — parse failures observed within a rolling window
    (default 60 s) exceed the configured count (default 5).

The evaluator owns no I/O — the consumer calls :meth:`record_message`
on each successful parse and :meth:`record_parse_failure` on errors, and
the audit loop calls :meth:`tick_with_sample` periodically with the
sample computed by :meth:`current_sample`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from petrosa_otel.evaluators import Evaluator

if TYPE_CHECKING:
    from petrosa_otel.evaluators.base import HysteresisPolicy
    from petrosa_otel.evaluators.publisher import VerdictPublisher


SUBSYSTEM = "ingest"

DEFAULT_SILENCE_THRESHOLD_S = 60
DEFAULT_STALENESS_THRESHOLD_S = 30
DEFAULT_INTEGRITY_WINDOW_S = 60
DEFAULT_INTEGRITY_FAILURE_BUDGET = 5


@dataclass
class _SubjectState:
    """Per-subject ingest state. Module-private; the evaluator owns it."""

    last_message_at: datetime | None = None
    last_payload_ts: datetime | None = None
    parse_failure_times: list[datetime] = field(default_factory=list)


class IngestEvaluator(Evaluator):
    """Subsystem evaluator for live-NATS ingest health (P2.2)."""

    def __init__(
        self,
        *,
        subjects: list[str],
        publisher: VerdictPublisher | None = None,
        hysteresis: HysteresisPolicy | None = None,
        silence_threshold_s: int = DEFAULT_SILENCE_THRESHOLD_S,
        staleness_threshold_s: int = DEFAULT_STALENESS_THRESHOLD_S,
        integrity_window_s: int = DEFAULT_INTEGRITY_WINDOW_S,
        integrity_failure_budget: int = DEFAULT_INTEGRITY_FAILURE_BUDGET,
        time_source: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            subsystem=SUBSYSTEM,
            publisher=publisher,
            hysteresis=hysteresis,
        )
        if not subjects:
            raise ValueError("at least one subject must be tracked")
        self._subjects = list(subjects)
        self._state: dict[str, _SubjectState] = {s: _SubjectState() for s in subjects}
        self._silence = timedelta(seconds=silence_threshold_s)
        self._staleness = timedelta(seconds=staleness_threshold_s)
        self._integrity_window = timedelta(seconds=integrity_window_s)
        self._integrity_budget = integrity_failure_budget
        self._time = time_source or (lambda: datetime.now(UTC))

    def record_message(
        self, subject: str, payload_timestamp: datetime | None = None
    ) -> None:
        """Hook: the consumer calls this on each successful message parse."""
        state = self._state.get(subject)
        if state is None:
            # Subject not tracked — silently ignore so the consumer is
            # never coupled to the evaluator's subject list.
            return
        now = self._time()
        state.last_message_at = now
        if payload_timestamp is not None:
            state.last_payload_ts = payload_timestamp

    def record_parse_failure(self, subject: str) -> None:
        """Hook: consumer calls this on each parse or schema error."""
        state = self._state.get(subject)
        if state is None:
            return
        now = self._time()
        cutoff = now - self._integrity_window
        # Drop expired entries before appending — keeps the list short.
        state.parse_failure_times = [
            t for t in state.parse_failure_times if t >= cutoff
        ]
        state.parse_failure_times.append(now)

    def current_sample(self) -> tuple[str, str]:
        """Compute the current (verdict, reason) sample without publishing.

        Exposed so the audit loop can decide when to tick, and so the
        unit tests can pin reason-string shape independently of hysteresis.
        Reason is bounded to ≤200 chars / single line per the framework's
        NFR-O5 publish contract.
        """
        now = self._time()
        worst_problem: str | None = None
        for subject in self._subjects:
            state = self._state[subject]

            if state.last_message_at is None:
                worst_problem = (
                    f"{subject}: no message yet"
                    if worst_problem is None
                    else worst_problem
                )
                continue

            silence = now - state.last_message_at
            if silence > self._silence:
                # Silence is the most actionable signal — surface it
                # first regardless of order.
                return "unhealthy", (
                    f"{subject}: silent for {int(silence.total_seconds())}s "
                    f"(threshold {int(self._silence.total_seconds())}s)"
                )

            if state.last_payload_ts is not None:
                staleness = now - state.last_payload_ts
                if staleness > self._staleness:
                    return "unhealthy", (
                        f"{subject}: payload {int(staleness.total_seconds())}s "
                        f"stale (threshold {int(self._staleness.total_seconds())}s)"
                    )

            cutoff = now - self._integrity_window
            recent_failures = sum(1 for t in state.parse_failure_times if t >= cutoff)
            if recent_failures > self._integrity_budget:
                return "unhealthy", (
                    f"{subject}: {recent_failures} parse failures in "
                    f"{int(self._integrity_window.total_seconds())}s "
                    f"(budget {self._integrity_budget})"
                )

        if worst_problem is not None:
            return "unknown", worst_problem
        return "healthy", "no silence, staleness, or integrity issues"

    async def evaluate(self) -> tuple[str, str]:
        return self.current_sample()
