"""Strategy-fidelity evaluator (P2.3, #594).

Compares per-strategy live signal behaviour to the strategy's persisted
characterization artifact (P3.2). Verdict downgrades to ``unhealthy``
when the divergence between live and characterized expected return
exceeds a configurable threshold over a rolling window.

The recommended metric in the ticket is rolling KL divergence between
live and backtest signal distributions, but the ticket itself notes
"subject to operator review". MVP uses a simpler, defensible metric
that the operator can evolve into KL once the substrate is in place:

  divergence = abs(live_mean_return - characterized_mean_return)
               / max(abs(characterized_mean_return), 1e-9)

The denominator floor prevents division-by-zero when the
characterization's expected mean return is exactly zero (rare but
possible). The metric is operator-review-pending — clearly documented
so the next iteration can swap in a richer distribution comparator
without rewriting the rest of the pipeline.

Live mean_return is computed from ``pnl_events`` where
``pnl_kind == "closed"``: ``mean(realized_pnl_usd / abs(reference_capital))``
when reference capital is supplied, otherwise the raw realized P&L mean
(operator can post-process). The MVP uses raw realized P&L mean — the
characterization stores mean_return in the same per-trade units when
populated by the standard backtest pipeline.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from data_manager.db.repositories.characterization_repository import (
    CharacterizationRepository,
)

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)


PNL_EVENTS_COLLECTION = "pnl_events"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"


@dataclass(slots=True)
class FidelityResult:
    """Outcome of one strategy-fidelity evaluation cycle.

    The shape mirrors what the verdict publisher sends on NATS and what
    the HTTP query endpoint returns — operators see one consistent
    payload across both surfaces.
    """

    strategy_id: str
    verdict: str  # healthy | unhealthy | unknown
    reason: str
    live_mean_return: float | None
    characterized_mean_return: float | None
    divergence: float | None
    threshold: float
    samples: int
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "live_mean_return": self.live_mean_return,
            "characterized_mean_return": self.characterized_mean_return,
            "divergence": self.divergence,
            "threshold": self.threshold,
            "samples": self.samples,
            "timestamp": self.timestamp.isoformat(),
        }


class FidelityService:
    """Compute strategy-fidelity verdicts from PnL + characterization."""

    # Minimum number of closed P&L events required before we trust the
    # live mean — below this the sample is too noisy to produce a
    # confident verdict, so we return UNKNOWN.
    MIN_SAMPLES_FOR_VERDICT = 10

    # Default tolerance: live mean must stay within this fraction of
    # characterized mean. 0.5 = up to 50% relative divergence is HEALTHY.
    DEFAULT_THRESHOLD = 0.5

    def __init__(
        self,
        mongodb_adapter: MongoDBAdapter,
        characterization_repository: CharacterizationRepository | None = None,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        min_samples: int = MIN_SAMPLES_FOR_VERDICT,
    ) -> None:
        self._mongo = mongodb_adapter
        self._char_repo = characterization_repository
        self._threshold = threshold
        self._min_samples = max(1, min_samples)

    async def evaluate(
        self,
        strategy_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> FidelityResult:
        """Compute the strategy-fidelity verdict for one strategy."""
        now = datetime.now(UTC)

        # Pull characterization first — without it we can't compute
        # divergence, so the verdict is UNKNOWN regardless of how many
        # live samples we have.
        characterized_mean = await self._fetch_characterized_mean_return(strategy_id)
        if characterized_mean is None:
            return FidelityResult(
                strategy_id=strategy_id,
                verdict=UNKNOWN,
                reason="no characterization available — cannot measure fidelity",
                live_mean_return=None,
                characterized_mean_return=None,
                divergence=None,
                threshold=self._threshold,
                samples=0,
                timestamp=now,
            )

        events = await self._fetch_closed_pnl_events(strategy_id, start=start, end=end)
        samples = len(events)
        if samples < self._min_samples:
            return FidelityResult(
                strategy_id=strategy_id,
                verdict=UNKNOWN,
                reason=(
                    f"only {samples} closed P&L events in window — need "
                    f"≥{self._min_samples} for a confident verdict"
                ),
                live_mean_return=None,
                characterized_mean_return=characterized_mean,
                divergence=None,
                threshold=self._threshold,
                samples=samples,
                timestamp=now,
            )

        live_mean = sum(events) / samples
        denominator = max(abs(characterized_mean), 1e-9)
        divergence = abs(live_mean - characterized_mean) / denominator

        if not math.isfinite(divergence):
            # Defensive — guards against weird float math when the
            # characterized mean is enormous or the live mean is NaN.
            return FidelityResult(
                strategy_id=strategy_id,
                verdict=UNKNOWN,
                reason="divergence not finite — skipping verdict",
                live_mean_return=live_mean,
                characterized_mean_return=characterized_mean,
                divergence=None,
                threshold=self._threshold,
                samples=samples,
                timestamp=now,
            )

        if divergence > self._threshold:
            return FidelityResult(
                strategy_id=strategy_id,
                verdict=UNHEALTHY,
                reason=(
                    f"divergence {divergence:.3f} exceeds threshold "
                    f"{self._threshold:.3f} (live mean_return {live_mean:.4f} "
                    f"vs characterized {characterized_mean:.4f} across "
                    f"{samples} closed events)"
                ),
                live_mean_return=live_mean,
                characterized_mean_return=characterized_mean,
                divergence=divergence,
                threshold=self._threshold,
                samples=samples,
                timestamp=now,
            )

        return FidelityResult(
            strategy_id=strategy_id,
            verdict=HEALTHY,
            reason=(
                f"divergence {divergence:.3f} within threshold "
                f"{self._threshold:.3f} ({samples} closed events)"
            ),
            live_mean_return=live_mean,
            characterized_mean_return=characterized_mean,
            divergence=divergence,
            threshold=self._threshold,
            samples=samples,
            timestamp=now,
        )

    async def _fetch_characterized_mean_return(self, strategy_id: str) -> float | None:
        if self._char_repo is None:
            return None
        try:
            artifact = await self._char_repo.get_latest(strategy_id)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "fidelity_characterization_fetch_failed",
                extra={"strategy_id": strategy_id, "error": str(e)},
            )
            return None
        if artifact is None:
            return None
        value = artifact.metrics.get("mean_return")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _fetch_closed_pnl_events(
        self,
        strategy_id: str,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[float]:
        """Pull realized P&L values for closed events.

        Only ``pnl_kind == "closed"`` events contribute — mark_to_market
        snapshots are noise here (the same open position re-snaps many
        times). Returns the raw ``realized_pnl_usd`` list; the caller
        averages.
        """
        if self._mongo is None or not getattr(self._mongo, "_connected", False):
            return []
        try:
            docs = await self._mongo.find_filtered(
                PNL_EVENTS_COLLECTION,
                filters={"strategy_id": strategy_id, "pnl_kind": "closed"},
                start=start,
                end=end,
                limit=1000,
                sort_field="timestamp",
                sort_order=1,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "fidelity_pnl_fetch_failed",
                extra={"strategy_id": strategy_id, "error": str(e)},
            )
            return []
        out: list[float] = []
        for d in docs:
            val = d.get("realized_pnl_usd")
            if val is None:
                continue
            try:
                out.append(float(val))
            except (TypeError, ValueError):
                continue
        return out
