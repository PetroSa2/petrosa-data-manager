"""Net R per closed round: the keep/kill input of the CIO (petrosa-data-manager#468, petrosa-cio#299).

A *round* is one cycle of a position as the round book replays it (petrosa-data-manager#546: hedge legs and
legacy exit sides handled). Its **net** is the realized P&L minus the fees of all its fills (entry and exit legs) in
the quote asset; a fill whose fee is missing or not in the quote asset is counted (``fee_unknown_fills``), never
guessed. Its **risk** is its entry notional x the stop distance the position carried (the stop actually placed, from
the position row, else the decision's); its **net R** is net / risk. The scorecard of ``scorecard_service`` owns
the scorecard routes and its metrics; this module is only the per-round net-R input of the keep/kill job. Funding
is not subtracted here (the scorecard's funding allocation is a separate step). Pure, Decimal throughout.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from data_manager.services.round_book import ClosedRound
from data_manager.services.scorecard_service import ZERO, decimal_string


@dataclass
class ScoredRound:
    strategy_id: str
    symbol: str
    closed_at: datetime
    gross: Decimal
    fees: Decimal
    fee_unknown_fills: int
    entry_notional: Decimal
    stop_fraction: Decimal | None
    net: Decimal = field(init=False)
    net_r: Decimal | None = field(init=False)

    def __post_init__(self) -> None:
        self.net = self.gross - self.fees
        risk = (
            self.entry_notional * self.stop_fraction
            if self.stop_fraction is not None
            else None
        )
        self.net_r = self.net / risk if risk and risk > 0 else None


def stop_fraction_of(entry_price: Any, stop_price: Any) -> Decimal | None:
    """|entry - stop| / entry from a position row, None when either is unusable."""
    try:
        entry, stop = Decimal(str(entry_price)), Decimal(str(stop_price))
    except Exception:
        return None
    if entry <= 0 or stop <= 0 or stop == entry:
        return None
    return abs(entry - stop) / entry


def score_rounds(
    rounds: list[ClosedRound],
    *,
    stops: Mapping[str, Decimal] | None = None,
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ScoredRound]:
    """Attach cost and risk to closed rounds, oldest close first.

    ``stops`` maps a position id to the stop fraction the position carried; ``decisions`` maps a decision id to
    ``{"stop_fraction": ...}`` (the fallback for the stop).
    """
    stops, decisions = stops or {}, decisions or {}
    out: list[ScoredRound] = []
    for r in rounds:
        stop = next((stops[p] for p in r.position_ids if p in stops), None)
        if stop is None:
            for decision_id in r.decision_ids:
                decision = decisions.get(decision_id)
                if decision:
                    stop = decision.get("stop_fraction")
                    break
        out.append(
            ScoredRound(
                strategy_id=r.strategy_id,
                symbol=r.symbol,
                closed_at=r.closed_at,
                gross=r.realized_dec,
                fees=r.fees,
                fee_unknown_fills=r.fee_unknown_fills,
                entry_notional=r.entry_notional,
                stop_fraction=stop,
            )
        )
    return sorted(out, key=lambda x: x.closed_at)


def strategy_net_r(rounds: list[ScoredRound]) -> dict[str, Any]:
    """The contract of the CIO keep/kill job (petrosa-cio#299).

    Per strategy: ``net_r`` per closed round, oldest first (rounds without a known risk are left out and counted
    in ``rounds_without_risk``), ``cumulative_net_loss_usd`` (the cumulative net P&L since the strategy's first
    round if negative, as a positive number; 0 when ahead) and ``closed_rounds``.
    """
    by_strategy: dict[str, list[ScoredRound]] = defaultdict(list)
    for r in rounds:
        by_strategy[r.strategy_id].append(r)
    out: dict[str, Any] = {}
    for strategy_id, rs in sorted(by_strategy.items()):
        total = sum((r.net for r in rs), ZERO)
        out[strategy_id] = {
            "net_r": [decimal_string(r.net_r) for r in rs if r.net_r is not None],
            "cumulative_net_loss_usd": decimal_string(max(ZERO, -total)),
            "closed_rounds": len(rs),
            "rounds_without_risk": sum(1 for r in rs if r.net_r is None),
            "fee_unknown_fills": sum(r.fee_unknown_fills for r in rs),
        }
    return {"strategies": out}
