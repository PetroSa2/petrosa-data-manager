"""Cost-aware strategy scorecard on the round book (petrosa-data-manager#468). Pure, Decimal throughout.

A *round* is one cycle of a position (the round book, petrosa-data-manager#546: hedge legs and legacy exit sides
handled). Its **net** is the realized P&L minus the fees of all its fills (entry and exit legs) in the quote
asset; a fill whose fee is missing or not in the quote asset is counted (``fee_unknown_fills``), never guessed.
A round's **risk** is its entry notional x the stop distance the position carried (the stop actually placed, from the
position row, else the decision's); its **net R** is net / risk. Rounds belong to the period of their **close time
(UTC)**. Its **funding** is its share of the exchange funding of the days it was open at a 00/08/16 UTC mark
(``scorecard_funding``, petrosa-data-manager#556), subtracted from the net, so net R and the keep/kill input
include it; ``scorecard_ledger`` reconciles the period with the ledger.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Any

from data_manager.services.round_book import ClosedRound, FillEntry

ZERO = Decimal("0")
UNKNOWN_MODE = "unknown"
GROUP_BYS = ("strategy", "strategy_symbol", "cio_mode")
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 468
Z_95 = Decimal("1.959963984540054")


def s(value: Decimal | None) -> str | None:
    """A Decimal as a plain string without trailing zeros (None stays None)."""
    if value is None:
        return None
    return format(value.normalize() if value != 0 else Decimal(0), "f")


@dataclass
class ScoredRound:
    strategy_id: str
    symbol: str
    position_side: str
    opened_at: datetime
    closed_at: datetime
    gross: Decimal
    fees: Decimal
    fee_unknown_fills: int
    entry_notional: Decimal
    stop_fraction: Decimal | None
    cio_mode: str
    position_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    funding: Decimal = ZERO  # cost (positive = paid) allocated to the round
    round_index: int = (
        -1
    )  # position in the closed-round list: the key of the funding allocation
    log: tuple[FillEntry, ...] = ()
    net: Decimal = field(init=False)
    net_r: Decimal | None = field(init=False)

    def __post_init__(self) -> None:
        self.net = self.gross - self.fees - self.funding
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
    funding_cost: Mapping[int, Decimal] | None = None,
) -> list[ScoredRound]:
    """Attach cost, risk and mode to closed rounds.

    ``stops`` maps a position id to the stop fraction the position carried; ``decisions`` maps a decision id to
    ``{"source": ..., "stop_fraction": ...}`` (the fallback for the stop, and the CIO mode).
    ``funding_cost`` maps a round's index in ``rounds`` to the funding it paid (positive = paid).
    """
    stops, decisions = stops or {}, decisions or {}
    funding_cost = funding_cost or {}
    out: list[ScoredRound] = []
    for index, r in enumerate(rounds):
        stop = next((stops[p] for p in r.position_ids if p in stops), None)
        mode = UNKNOWN_MODE
        for decision_id in r.decision_ids:
            decision = decisions.get(decision_id)
            if decision:
                if stop is None:
                    stop = decision.get("stop_fraction")
                mode = str(decision.get("source") or mode)
                break
        out.append(
            ScoredRound(
                strategy_id=r.strategy_id,
                symbol=r.symbol,
                position_side=r.position_side,
                opened_at=r.opened_at,
                closed_at=r.closed_at,
                gross=r.realized_dec,
                fees=r.fees,
                fee_unknown_fills=r.fee_unknown_fills,
                entry_notional=r.entry_notional,
                stop_fraction=stop,
                cio_mode=mode,
                position_ids=r.position_ids,
                decision_ids=r.decision_ids,
                funding=funding_cost.get(index, ZERO),
                round_index=index,
                log=r.log,
            )
        )
    return sorted(out, key=lambda x: x.closed_at)


def in_period(
    rounds: list[ScoredRound], start: datetime | None, end: datetime | None
) -> list[ScoredRound]:
    """Rounds closed in [start, end), by the close time in UTC."""

    def utc(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    return [
        r
        for r in rounds
        if (start is None or utc(r.closed_at) >= utc(start))
        and (end is None or utc(r.closed_at) < utc(end))
    ]


def group_key(r: ScoredRound, group_by: str) -> str:
    if group_by == "strategy_symbol":
        return f"{r.strategy_id}/{r.symbol}"
    if group_by == "cio_mode":
        return r.cio_mode
    return r.strategy_id


def wilson(wins: int, n: int) -> dict[str, Any]:
    """The 95% Wilson interval of a win rate, with n."""
    if n == 0:
        return {"n": 0, "low": None, "high": None}
    p, nn = Decimal(wins) / Decimal(n), Decimal(n)
    denominator = 1 + Z_95 * Z_95 / nn
    centre = (p + Z_95 * Z_95 / (2 * nn)) / denominator
    spread = (
        Z_95 * (p * (1 - p) / nn + Z_95 * Z_95 / (4 * nn * nn)).sqrt() / denominator
    )
    return {
        "n": n,
        "low": s(max(ZERO, centre - spread)),
        "high": s(min(Decimal(1), centre + spread)),
    }


def bootstrap_mean(values: list[Decimal]) -> dict[str, Any]:
    """The 95% bootstrap interval of the mean (seeded: the same input gives the same interval), with n."""
    n = len(values)
    if n == 0:
        return {"n": 0, "low": None, "high": None}
    rng = random.Random(BOOTSTRAP_SEED)
    means = sorted(
        sum((values[rng.randrange(n)] for _ in range(n)), ZERO) / n
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    return {
        "n": n,
        "low": s(means[int(0.025 * BOOTSTRAP_RESAMPLES)]),
        "high": s(means[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]),
    }


def _median(values: list[Decimal]) -> Decimal | None:
    return median(values) if values else None


def metrics(rounds: list[ScoredRound], minimum: int | None) -> dict[str, Any]:
    """Per-group metrics, net of fees. ``rounds`` are ordered by close time."""
    n = len(rounds)
    nets = [r.net for r in rounds]
    gross = sum((r.gross for r in rounds), ZERO)
    fees = sum((r.fees for r in rounds), ZERO)
    net = sum(nets, ZERO)
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v < 0]
    payoff = (
        (sum(wins, ZERO) / len(wins)) / (abs(sum(losses, ZERO)) / len(losses))
        if wins and losses
        else None
    )
    expectancy = net / n if n else None
    ex_top1 = None
    if n > 1:
        rest = list(nets)
        rest.remove(max(rest))
        ex_top1 = sum(rest, ZERO) / len(rest)
    running = peak = drawdown = ZERO
    for value in nets:  # by close time
        running += value
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    r_values = [r.net_r for r in rounds if r.net_r is not None]
    return {
        "n_trades": n,
        "gross_pnl": s(gross),
        "fees": s(fees),
        "funding_allocated": s(sum((r.funding for r in rounds), ZERO)),
        "net_pnl": s(net),
        "win_rate": s(Decimal(len(wins)) / n) if n else None,
        "avg_win": s(sum(wins, ZERO) / len(wins)) if wins else None,
        "avg_loss": s(sum(losses, ZERO) / len(losses)) if losses else None,
        "payoff": s(payoff),
        "expectancy_per_trade": s(expectancy),
        "expectancy_ex_top1": s(ex_top1),
        "median_trade": s(_median(nets)),
        "max_drawdown": s(drawdown),
        "fee_share_of_gross": s(fees / abs(gross)) if gross else None,
        "sample_ok": None if minimum is None else n >= minimum,
        "win_rate_interval": wilson(len(wins), n),
        "expectancy_interval": bootstrap_mean(nets),
        "net_r": {
            "n": len(r_values),
            "mean": s(sum(r_values, ZERO) / len(r_values)) if r_values else None,
            "rounds_without_risk": n - len(r_values),
        },
        "fee_unknown_fills": sum(r.fee_unknown_fills for r in rounds),
    }


def scorecard(
    rounds: list[ScoredRound],
    group_by: str,
    minimum: int | None,
    open_positions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The scorecard: metrics per group plus the root totals."""
    grouped: dict[str, list[ScoredRound]] = defaultdict(list)
    for r in rounds:
        grouped[group_key(r, group_by)].append(r)
    groups = {key: metrics(rs, minimum) for key, rs in sorted(grouped.items())}
    for key, count in (open_positions or {}).items():
        groups.setdefault(key, metrics([], minimum))
        groups[key]["open_positions"] = count
    return {
        "group_by": group_by,
        "groups": groups,
        "total_net": s(sum((r.net for r in rounds), ZERO)),
        "total_fees": s(sum((r.fees for r in rounds), ZERO)),
        "total_funding": s(sum((r.funding for r in rounds), ZERO)),
    }


def strategy_net_r(rounds: list[ScoredRound]) -> dict[str, Any]:
    """The contract of the CIO keep/kill job (petrosa-cio#299).

    Per strategy: ``net_r`` per closed round, oldest first (rounds without a known risk are left out and
    counted in ``rounds_without_risk``), ``cumulative_net_loss_usd`` (the cumulative net P&L since the
    strategy's first round if negative, as a positive number; 0 when ahead) and ``closed_rounds``.
    """
    by_strategy: dict[str, list[ScoredRound]] = defaultdict(list)
    for r in rounds:
        by_strategy[r.strategy_id].append(r)
    out: dict[str, Any] = {}
    for strategy_id, rs in sorted(by_strategy.items()):
        total = sum((r.net for r in rs), ZERO)
        out[strategy_id] = {
            "net_r": [s(r.net_r) for r in rs if r.net_r is not None],
            "cumulative_net_loss_usd": s(max(ZERO, -total)),
            "closed_rounds": len(rs),
            "rounds_without_risk": sum(1 for r in rs if r.net_r is None),
            "fee_unknown_fills": sum(r.fee_unknown_fills for r in rs),
        }
    return {"strategies": out}


def evaluate(
    groups: Mapping[str, Mapping[str, Any]],
    *,
    min_trades: int | None,
    min_expectancy_net: Decimal | None,
    max_dd_fraction: Decimal | None,
    capital: Decimal | None = None,
) -> dict[str, Any]:
    """keep / watch / disable per group from thresholds that must be configured: with any unset the status is
    ``unconfigured`` (never a code default). A group below the sample minimum is ``watch``, never ``disable``.
    The evaluation only reports."""
    if min_trades is None or min_expectancy_net is None or max_dd_fraction is None:
        return {
            "status": "unconfigured",
            "reason": "scorecard_min_trades, scorecard_min_expectancy_net and scorecard_max_dd_fraction must be set",
            "groups": {},
        }
    result: dict[str, Any] = {}
    for key, m in groups.items():
        n = m["n_trades"]
        expectancy = (
            Decimal(m["expectancy_per_trade"])
            if m["expectancy_per_trade"] is not None
            else None
        )
        dd = Decimal(m["max_drawdown"]) if m["max_drawdown"] is not None else ZERO
        if n < min_trades:
            status, reason = "watch", "sample_below_minimum"
        elif expectancy is not None and expectancy < min_expectancy_net:
            status, reason = "disable", "expectancy_below_minimum"
        elif capital and dd / capital > max_dd_fraction:
            status, reason = "disable", "drawdown_above_maximum"
        else:
            status, reason = "keep", "within_thresholds"
        result[key] = {
            "status": status,
            "reason": reason,
            "n": n,
            "expectancy_per_trade": m["expectancy_per_trade"],
        }
    return {"status": "evaluated", "groups": result}
