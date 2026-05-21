"""Portfolio drawdown vs characterization envelope (P4.2, #602).

Computes peak-to-current equity drawdown for a strategy from the
``pnl_events`` stream that P4.1 (#601) persists, then compares the
current drawdown to the percentile distribution captured in the
strategy's most recent characterization (``drawdown_envelope``,
populated by P3.2).

Breach semantics (from FR30):
  * "envelope breach" = current drawdown exceeds the configured
    envelope percentile (default ``p99`` — the second-to-last entry of
    a 4-percentile envelope ``[p50, p90, p99, p100]``).
  * The breach payload carries enough context for CIO to act: the
    strategy, the current drawdown %, the breached threshold %, the
    percentile label, and equity snapshots.

The service is a pure compute layer — wiring the periodic check + NATS
publish lives in ``DrawdownScheduler``. The HTTP endpoint
(``/api/v1/portfolio/drawdown``) calls compute directly so operators
can poll on-demand without waiting for the next scheduler tick.
"""

from __future__ import annotations

import logging
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

# Default percentile to use as the breach threshold. The characterization
# envelope is stored as ``[p50, p90, p99, p100]`` per the P3.2 schema —
# index 2 = p99 (single-worst-case threshold). Operators can override
# via env / scheduler config.
DEFAULT_BREACH_PERCENTILE_INDEX = 2
DEFAULT_BREACH_PERCENTILE_LABEL = "p99"


@dataclass(slots=True)
class DrawdownResult:
    """Outcome of one drawdown evaluation cycle.

    All percentages are positive numbers in [0, 100] (drawdowns are
    expressed as "% loss from peak", not signed P&L). Equity values are
    in USD per the PnlEvent schema.
    """

    strategy_id: str
    current_drawdown_pct: float
    envelope_threshold_pct: float | None
    breach_percentile: str | None
    breached: bool
    peak_equity_usd: float
    current_equity_usd: float
    events_evaluated: int
    timestamp: datetime
    reason: str
    # The full envelope is included for transparency — the dashboard
    # surfaces the whole percentile band, not just the threshold the
    # service compared against.
    envelope: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "current_drawdown_pct": self.current_drawdown_pct,
            "envelope_threshold_pct": self.envelope_threshold_pct,
            "breach_percentile": self.breach_percentile,
            "breached": self.breached,
            "peak_equity_usd": self.peak_equity_usd,
            "current_equity_usd": self.current_equity_usd,
            "events_evaluated": self.events_evaluated,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
            "envelope": self.envelope,
        }


class DrawdownService:
    """Compute strategy-scoped drawdown and compare to the envelope.

    The service is stateless — every call queries fresh PnL events
    from MongoDB. Callers that need cached results should layer that
    on top.
    """

    def __init__(
        self,
        mongodb_adapter: MongoDBAdapter,
        characterization_repository: CharacterizationRepository | None = None,
        *,
        breach_percentile_index: int = DEFAULT_BREACH_PERCENTILE_INDEX,
        breach_percentile_label: str = DEFAULT_BREACH_PERCENTILE_LABEL,
    ) -> None:
        self._mongo = mongodb_adapter
        self._char_repo = characterization_repository
        self._breach_index = breach_percentile_index
        self._breach_label = breach_percentile_label

    async def compute(
        self,
        strategy_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> DrawdownResult:
        """Compute peak-to-current drawdown for one strategy.

        The equity series is reconstructed event-by-event:

        * ``pnl_kind == "closed"`` events contribute ``realized_pnl_usd``
          cumulatively (closed positions are realized once).
        * ``pnl_kind == "mark_to_market"`` events SET the unrealized
          component to ``unrealized_pnl_usd`` at that point (subsequent
          mark-to-market events overwrite — they're snapshots, not
          deltas).
        * ``pnl_kind == "aggregate"`` events are folded into realized
          (data_manager already pre-aggregated; trust the sum).

        ``equity_at(t) = cumulative_realized(t) + latest_unrealized(t)``

        Drawdown = ``(peak_equity - current_equity) / peak_equity * 100``
        when ``peak_equity > 0``; clamped to 0 otherwise (no
        meaningful drawdown when equity has never been positive).
        """
        events = await self._fetch_events(strategy_id, start=start, end=end)
        now = datetime.now(UTC)

        if not events:
            return DrawdownResult(
                strategy_id=strategy_id,
                current_drawdown_pct=0.0,
                envelope_threshold_pct=None,
                breach_percentile=None,
                breached=False,
                peak_equity_usd=0.0,
                current_equity_usd=0.0,
                events_evaluated=0,
                timestamp=now,
                reason="no pnl events in window",
                envelope=await self._fetch_envelope(strategy_id),
            )

        cumulative_realized = 0.0
        latest_unrealized = 0.0
        peak_equity = 0.0
        current_equity = 0.0

        for ev in events:
            kind = (ev.get("pnl_kind") or "").lower()
            realized = float(ev.get("realized_pnl_usd") or 0.0)
            unrealized = float(ev.get("unrealized_pnl_usd") or 0.0)
            if kind == "closed":
                cumulative_realized += realized
            elif kind == "mark_to_market":
                latest_unrealized = unrealized
            elif kind == "aggregate":
                # Aggregate rows carry pre-summed totals; trust them.
                cumulative_realized += realized
                if unrealized != 0.0:
                    latest_unrealized = unrealized
            else:
                # Unknown pnl_kind: fall back to whichever field is set.
                if realized:
                    cumulative_realized += realized
                if unrealized:
                    latest_unrealized = unrealized

            equity = cumulative_realized + latest_unrealized
            current_equity = equity
            if equity > peak_equity:
                peak_equity = equity

        drawdown_pct = 0.0
        if peak_equity > 0:
            drawdown_pct = max(
                0.0,
                (peak_equity - current_equity) / peak_equity * 100.0,
            )

        envelope = await self._fetch_envelope(strategy_id)
        threshold_pct, label = self._select_threshold(envelope)

        breached = threshold_pct is not None and drawdown_pct > threshold_pct
        if breached:
            reason = (
                f"current drawdown {drawdown_pct:.2f}% exceeds "
                f"envelope {label} threshold {threshold_pct:.2f}%"
            )
        elif threshold_pct is None:
            reason = (
                f"current drawdown {drawdown_pct:.2f}% — no envelope "
                "available for breach comparison"
            )
        else:
            reason = (
                f"current drawdown {drawdown_pct:.2f}% within envelope "
                f"{label} threshold {threshold_pct:.2f}%"
            )

        return DrawdownResult(
            strategy_id=strategy_id,
            current_drawdown_pct=drawdown_pct,
            envelope_threshold_pct=threshold_pct,
            breach_percentile=label if threshold_pct is not None else None,
            breached=breached,
            peak_equity_usd=peak_equity,
            current_equity_usd=current_equity,
            events_evaluated=len(events),
            timestamp=now,
            reason=reason,
            envelope=envelope,
        )

    async def _fetch_events(
        self,
        strategy_id: str,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[dict]:
        """Pull PnL events for one strategy, oldest first.

        Newest-first ordering would break the cumulative replay — we
        sort by timestamp ASC explicitly even though MongoDB defaults
        vary by index direction.
        """
        if self._mongo is None or not getattr(self._mongo, "_connected", False):
            return []
        try:
            query: dict[str, Any] = {"strategy_id": strategy_id}
            if start is not None or end is not None:
                window: dict[str, Any] = {}
                if start is not None:
                    window["$gte"] = start
                if end is not None:
                    window["$lt"] = end
                query["timestamp"] = window
            coll = self._mongo.db[PNL_EVENTS_COLLECTION]
            cursor = coll.find(query).sort("timestamp", 1)
            docs = await cursor.to_list(length=None)
            for d in docs:
                d.pop("_id", None)
            return docs
        except Exception as e:  # noqa: BLE001 — surface failures as empty + log
            logger.error(
                "drawdown_pnl_fetch_failed",
                extra={"strategy_id": strategy_id, "error": str(e)},
            )
            return []

    async def _fetch_envelope(self, strategy_id: str) -> list[float] | None:
        if self._char_repo is None:
            return None
        artifact = await self._char_repo.get_latest(strategy_id)
        if artifact is None:
            return None
        # Defensive copy; the dashboard surfaces this directly.
        return list(artifact.drawdown_envelope)

    def _select_threshold(
        self, envelope: list[float] | None
    ) -> tuple[float | None, str]:
        """Pick the threshold percentile value from the envelope.

        Returns ``(threshold_pct, label)`` — ``threshold_pct`` is ``None``
        when no envelope is available or the configured index is out of
        range. Label is the percentile name (e.g., ``"p99"``).
        """
        if not envelope:
            return None, self._breach_label
        idx = self._breach_index
        if idx < 0 or idx >= len(envelope):
            # Fall back to the last (worst-case) percentile if the
            # configured index doesn't fit. The envelope length varies
            # across characterizations, so a hard out-of-range error
            # would just disable breach checks for non-standard runs.
            idx = len(envelope) - 1
            label = f"p[{idx}]"
        else:
            label = self._breach_label
        return float(envelope[idx]), label
