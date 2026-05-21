"""NATS verdict publisher for strategy-fidelity (P2.3, #594).

Publishes per-strategy verdicts on
``evaluator.strategy.<strategy_id>.verdict`` per the P2.1 evaluator
framework convention. The payload is JSON ``{"verdict", "reason"}`` —
identical shape to ingest/audit/cio evaluators so the CIO
EvaluatorSubscriber (#597) can consume it without extending its
parser.

Hysteresis is owned by the publisher rather than the service: the
service computes a fresh verdict on every tick (stateless), and the
publisher only emits when (a) the verdict has been stable for
``stable_ticks_required`` consecutive ticks AND (b) the emitted
verdict has actually changed. This prevents both flapping AND constant
re-emission of the same healthy verdict.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_manager.consumer.nats_client import NATSClient
    from data_manager.strategies.fidelity_service import FidelityResult

logger = logging.getLogger(__name__)


VERDICT_SUBJECT_PREFIX = "evaluator.strategy"


class FidelityVerdictPublisher:
    """Per-strategy NATS verdict publisher with hysteresis."""

    def __init__(
        self,
        nats_client: NATSClient,
        *,
        stable_ticks_required: int = 2,
    ) -> None:
        self._nc = nats_client
        self._stable_ticks_required = max(1, stable_ticks_required)
        # strategy_id -> (last_emitted_verdict, candidate_verdict, candidate_streak)
        self._state: dict[str, tuple[str | None, str, int]] = {}

    async def maybe_publish(self, result: FidelityResult) -> bool:
        """Apply hysteresis, then publish iff verdict has flipped.

        Returns True iff a publish was attempted (the underlying client
        may drop on disconnect; that's logged but not raised).
        """
        sid = result.strategy_id
        raw_verdict = result.verdict
        last_emitted, candidate, streak = self._state.get(sid, (None, raw_verdict, 0))

        # Same as the externally-emitted verdict? Reset candidate streak
        # — we're already in the stable state.
        if raw_verdict == last_emitted:
            self._state[sid] = (last_emitted, raw_verdict, 0)
            return False

        # New raw verdict — restart or continue the candidate streak.
        if raw_verdict == candidate:
            streak += 1
        else:
            candidate = raw_verdict
            streak = 1

        if streak < self._stable_ticks_required:
            self._state[sid] = (last_emitted, candidate, streak)
            return False

        # Streak satisfied — promote to emitted.
        subject = f"{VERDICT_SUBJECT_PREFIX}.{sid}.verdict"
        payload = json.dumps(
            {"verdict": raw_verdict, "reason": result.reason[:200]}
        ).encode()
        try:
            await self._nc.publish(subject, payload)
            logger.info(
                "fidelity_verdict_published",
                extra={
                    "subject": subject,
                    "strategy_id": sid,
                    "verdict": raw_verdict,
                    "previous": last_emitted,
                },
            )
        except Exception as exc:  # noqa: BLE001 — scheduler must survive
            logger.error(
                "fidelity_verdict_publish_failed",
                extra={"subject": subject, "error": str(exc)},
            )
            # Reset hysteresis on publish failure so we retry on the next
            # tick — don't lose the flip just because NATS hiccupped.
            self._state[sid] = (last_emitted, candidate, streak - 1)
            return False

        self._state[sid] = (raw_verdict, raw_verdict, 0)
        return True
