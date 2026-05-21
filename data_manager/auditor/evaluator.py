"""Gap-detector evaluator — consumer-side proof of the P2.1 framework (#634).

Wraps the existing :class:`GapDetector` and publishes a single
``healthy`` / ``unhealthy`` verdict per audit cycle on
``evaluator.data-manager.verdict`` via the shared
:mod:`petrosa_otel.evaluators` framework (P2.1, petrosa_k8s#592).

The wrapper holds no business logic — it converts an aggregate count of
gaps detected in the most recent cycle into the framework's
``(verdict, reason)`` tuple. Hysteresis (default n=3) is the framework's
job; this class only feeds samples and reports the worst observed gap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from petrosa_otel.evaluators import Evaluator

if TYPE_CHECKING:
    from petrosa_otel.evaluators.base import HysteresisPolicy
    from petrosa_otel.evaluators.publisher import VerdictPublisher


SUBSYSTEM = "data-manager"
REASON_HEALTHY = "no gaps in audit cycle"
# Reason length is bounded by the framework (NFR-O5, ≤200 chars, single line).
# We format conservatively so even a worst-case reason stays well under that.
REASON_TEMPLATE_UNHEALTHY = (
    "{count} gap(s) detected in cycle (worst: {symbol} {timeframe}, {duration_s}s)"
)


class GapDetectorEvaluator(Evaluator):
    """Subsystem evaluator wrapping :class:`GapDetector`.

    The audit scheduler calls :meth:`record_cycle` at the end of every
    cycle with the cycle's aggregate gap state, then calls
    :meth:`tick_with_sample` (inherited) to push that sample through the
    hysteresis policy and publish.
    """

    def __init__(
        self,
        *,
        publisher: VerdictPublisher | None = None,
        hysteresis: HysteresisPolicy | None = None,
    ) -> None:
        super().__init__(
            subsystem=SUBSYSTEM,
            publisher=publisher,
            hysteresis=hysteresis,
        )

    @staticmethod
    def cycle_sample(
        total_gaps: int,
        worst_gap_summary: str | None = None,
    ) -> tuple[str, str]:
        """Convert a cycle's aggregate state into a (verdict, reason) tuple.

        Exposed as a staticmethod so the scheduler can build the sample
        without depending on an instance, and so the unit tests can pin
        the reason-string shape independently of hysteresis behavior.
        """
        if total_gaps <= 0:
            return "healthy", REASON_HEALTHY
        reason = (
            f"{total_gaps} gap(s) detected in cycle (worst: {worst_gap_summary})"
            if worst_gap_summary
            else f"{total_gaps} gap(s) detected in cycle"
        )
        return "unhealthy", reason
