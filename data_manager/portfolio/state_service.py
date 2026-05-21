"""Portfolio state-at-time-T query (P4.4, #604).

Answers the operator question "why did the portfolio do X at time T?"
by reconstructing two things for a given timestamp ``T``:

  1. **Portfolio state at T** — cumulative realized P&L, latest
     mark-to-market unrealized P&L, peak equity, current drawdown,
     and a sketch of open positions (filled minus closed via the
     execution_events stream).

  2. **Event chain leading up to T** — the last N decisions, the last
     M execution events, and the last K P&L events, all timestamp
     ``< T`` and ordered oldest-first within their slice so the
     operator can read the chain forward.

Optional ``strategy_id`` filter scopes both the state and the chain
to a single strategy. Without it, the response is portfolio-wide.

The endpoint is operator-facing and read-only. It composes the
existing storage primitives (``pnl_events``, ``execution_events``,
``cio_decisions``) via ``MongoDBAdapter.find_filtered`` — no new
collection or schema needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

if TYPE_CHECKING:
    from data_manager.db.mongodb_adapter import MongoDBAdapter

logger = logging.getLogger(__name__)


PNL_EVENTS_COLLECTION = "pnl_events"
EXECUTION_EVENTS_COLLECTION = "execution_events"
CIO_DECISIONS_COLLECTION = "cio_decisions"

DEFAULT_DECISIONS_IN_CHAIN = 20
DEFAULT_EXECUTIONS_IN_CHAIN = 50
DEFAULT_PNL_EVENTS_IN_CHAIN = 50


@dataclass(slots=True)
class PortfolioStateAtTime:
    """Portfolio state + event chain at time T.

    Equity values are in USD per the PnlEvent schema. Drawdown is
    expressed as positive percent off-peak (consistent with #602's
    DrawdownResult).
    """

    at: datetime
    strategy_id: str | None
    cumulative_realized_pnl_usd: float
    latest_unrealized_pnl_usd: float
    current_equity_usd: float
    peak_equity_usd: float
    current_drawdown_pct: float
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    recent_decisions: list[dict[str, Any]] = field(default_factory=list)
    recent_executions: list[dict[str, Any]] = field(default_factory=list)
    recent_pnl_events: list[dict[str, Any]] = field(default_factory=list)
    events_evaluated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "strategy_id": self.strategy_id,
            "cumulative_realized_pnl_usd": self.cumulative_realized_pnl_usd,
            "latest_unrealized_pnl_usd": self.latest_unrealized_pnl_usd,
            "current_equity_usd": self.current_equity_usd,
            "peak_equity_usd": self.peak_equity_usd,
            "current_drawdown_pct": self.current_drawdown_pct,
            "open_positions": self.open_positions,
            "recent_decisions": self.recent_decisions,
            "recent_executions": self.recent_executions,
            "recent_pnl_events": self.recent_pnl_events,
            "events_evaluated": self.events_evaluated,
        }


class PortfolioStateService:
    """Computes portfolio state + event chain at a given timestamp."""

    def __init__(
        self,
        mongodb_adapter: MongoDBAdapter,
        *,
        decisions_in_chain: int = DEFAULT_DECISIONS_IN_CHAIN,
        executions_in_chain: int = DEFAULT_EXECUTIONS_IN_CHAIN,
        pnl_events_in_chain: int = DEFAULT_PNL_EVENTS_IN_CHAIN,
    ) -> None:
        self._mongo = mongodb_adapter
        self._decisions_in_chain = max(1, decisions_in_chain)
        self._executions_in_chain = max(1, executions_in_chain)
        self._pnl_events_in_chain = max(1, pnl_events_in_chain)

    async def state_at(
        self,
        at: datetime,
        *,
        strategy_id: str | None = None,
    ) -> PortfolioStateAtTime:
        """Compute the portfolio state at ``at`` plus the chain leading up to it."""
        pnl_filters: dict[str, Any] = {}
        if strategy_id is not None:
            pnl_filters["strategy_id"] = strategy_id

        # Pull every pnl_event up to T (oldest first) for cumulative
        # equity reconstruction. The same chronological replay shape as
        # DrawdownService.compute (#602), bounded by ``at`` instead of a
        # rolling window.
        pnl_events = await self._fetch_window(
            PNL_EVENTS_COLLECTION,
            filters=pnl_filters,
            end=at,
            limit=5000,
            sort_order=1,  # oldest first
        )

        (
            cumulative_realized,
            latest_unrealized,
            current_equity,
            peak_equity,
            drawdown_pct,
        ) = self._replay_equity(pnl_events)

        # Pull execution events and decisions up to T (newest first,
        # then slice + reverse for the chain). Newest-first query lets
        # the limit cap the most recent slice naturally.
        exec_filters: dict[str, Any] = {}
        if strategy_id is not None:
            exec_filters["strategy_id"] = strategy_id

        recent_executions_desc = await self._fetch_window(
            EXECUTION_EVENTS_COLLECTION,
            filters=exec_filters,
            end=at,
            limit=self._executions_in_chain,
            sort_order=-1,  # newest first
        )
        recent_executions = list(reversed(recent_executions_desc))

        dec_filters: dict[str, Any] = {}
        if strategy_id is not None:
            dec_filters["strategy_id"] = strategy_id
        recent_decisions_desc = await self._fetch_window(
            CIO_DECISIONS_COLLECTION,
            filters=dec_filters,
            end=at,
            limit=self._decisions_in_chain,
            sort_order=-1,
        )
        recent_decisions = list(reversed(recent_decisions_desc))

        # The recent P&L slice is independent of the cumulative replay
        # — operators may want a tail-of-N P&L events even when the
        # cumulative scan walked thousands. Take from the tail of the
        # already-fetched pnl_events to avoid a second query.
        recent_pnl_events = pnl_events[-self._pnl_events_in_chain :]

        open_positions = self._infer_open_positions(recent_executions_desc)

        return PortfolioStateAtTime(
            at=at,
            strategy_id=strategy_id,
            cumulative_realized_pnl_usd=cumulative_realized,
            latest_unrealized_pnl_usd=latest_unrealized,
            current_equity_usd=current_equity,
            peak_equity_usd=peak_equity,
            current_drawdown_pct=drawdown_pct,
            open_positions=open_positions,
            recent_decisions=recent_decisions,
            recent_executions=recent_executions,
            recent_pnl_events=recent_pnl_events,
            events_evaluated=len(pnl_events),
        )

    def _replay_equity(
        self, pnl_events: list[dict[str, Any]]
    ) -> tuple[float, float, float, float, float]:
        """Chronologically replay the equity series. Same rules as #602's
        DrawdownService — realized accumulates, mark_to_market overwrites
        the unrealized component, aggregate folds into realized.

        Returns: (cumulative_realized, latest_unrealized, current_equity,
        peak_equity, drawdown_pct).
        """
        cumulative_realized = 0.0
        latest_unrealized = 0.0
        peak_equity = 0.0
        current_equity = 0.0
        for ev in pnl_events:
            kind = (ev.get("pnl_kind") or "").lower()
            realized = float(ev.get("realized_pnl_usd") or 0.0)
            unrealized = float(ev.get("unrealized_pnl_usd") or 0.0)
            if kind == "closed":
                cumulative_realized += realized
            elif kind == "mark_to_market":
                latest_unrealized = unrealized
            elif kind == "aggregate":
                cumulative_realized += realized
                if unrealized != 0.0:
                    latest_unrealized = unrealized
            else:
                if realized:
                    cumulative_realized += realized
                if unrealized:
                    latest_unrealized = unrealized
            current_equity = cumulative_realized + latest_unrealized
            if current_equity > peak_equity:
                peak_equity = current_equity
        drawdown_pct = 0.0
        if peak_equity > 0:
            drawdown_pct = max(
                0.0,
                (peak_equity - current_equity) / peak_equity * 100.0,
            )
        return (
            cumulative_realized,
            latest_unrealized,
            current_equity,
            peak_equity,
            drawdown_pct,
        )

    def _infer_open_positions(
        self, executions_desc: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Sketch of which orders look "still open" at ``at``.

        Heuristic: an order_id is open iff its most recent
        ``event_type`` (looking newest-first) is ``"filled"`` or
        ``"partial_fill"`` AND we have NOT yet seen a later
        ``"closed"`` / ``"cancelled"`` for it. This is a sketch — the
        execution event stream may have multiple intermediate states
        between fill and close, and a fully accurate position read
        would replay the entire history. For the operator's "why at T"
        view, the recent-slice sketch is enough to surface "look, here
        are the strategies with open exposure right now".
        """
        seen: dict[str, dict[str, Any]] = {}
        for ev in executions_desc:
            oid = ev.get("order_id")
            if not oid or oid in seen:
                continue
            evt = (ev.get("event_type") or "").lower()
            if evt in ("filled", "partial_fill"):
                seen[oid] = {
                    "order_id": oid,
                    "strategy_id": ev.get("strategy_id"),
                    "symbol": ev.get("symbol"),
                    "event_type": evt,
                    "timestamp": _maybe_iso(ev.get("timestamp")),
                }
            else:
                # Mark as "seen but closed/cancelled" — skip it from
                # the open list.
                seen[oid] = {}
        return [v for v in seen.values() if v]

    async def _fetch_window(
        self,
        collection: str,
        *,
        filters: dict[str, Any],
        end: datetime,
        limit: int,
        sort_order: int,
    ) -> list[dict[str, Any]]:
        if self._mongo is None or not getattr(self._mongo, "_connected", False):
            return []
        try:
            return await self._mongo.find_filtered(
                collection,
                filters=filters,
                end=end,
                limit=limit,
                sort_field="timestamp",
                sort_order=sort_order,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "portfolio_state_fetch_failed",
                extra={"collection": collection, "error": str(e)},
            )
            return []


def _maybe_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None
