"""Funding allocation for the scorecard (petrosa-data-manager#556). Pure, Decimal throughout.

The exchange books funding per symbol and UTC day (``ledger_exchange_daily.funding_fee``, income: negative means
paid). Funding is charged only to positions open at the funding marks, 00:00, 08:00 and 16:00 UTC. A round is
*open at a mark* when ``opened_at <= mark < closed_at`` (an open round has no close).

For one symbol and day with total ``T`` the allocation assumes one rate over the day's marks, so each mark's
funding is proportional to the entry notional open at it. A round's share of ``T`` is therefore its entry
notional x the number of that day's marks it was open at (notional-time) over the same sum across all rounds.
The share is exact: it is split in Decimal and the rounding remainder goes to the largest weight, so the shares
add up to ``T``. When no known round is open at any mark of that symbol and day (a position the book does not
know), ``T`` is *unallocated* and stays visible; nothing is spread over rounds that did not hold a position.

Limits, reported and not hidden: the exchange total is per symbol and day, not per side, so on a symbol-day
where a LONG and a SHORT leg are open at the same mark the allocation by notional ignores that opposite
sides pay and receive opposite funding (``hedged_symbol_days`` counts them); the notional is the entry
notional, not the mark-to-market value at the funding time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

ZERO = Decimal("0")
MARK_HOURS = (0, 8, 16)
_QUANTUM = Decimal("1e-10")


@dataclass(frozen=True)
class Exposure:
    """A round, closed or open, as far as funding is concerned."""

    key: int
    symbol: str
    opened_at: datetime
    closed_at: datetime | None
    entry_notional: Decimal
    position_side: str = "NET"


@dataclass
class FundingAllocation:
    """Funding income (negative = paid) per round and day, and what no round held a position for."""

    by_round_day: dict[tuple[int, date], Decimal] = field(default_factory=dict)
    unallocated: dict[tuple[str, date], Decimal] = field(default_factory=dict)
    hedged_symbol_days: set[tuple[str, date]] = field(default_factory=set)

    def income_by_round(self) -> dict[int, Decimal]:
        """Funding income of each round over its whole life (negative = paid)."""
        totals: dict[int, Decimal] = defaultdict(lambda: ZERO)
        for (key, _day), value in self.by_round_day.items():
            totals[key] += value
        return dict(totals)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def marks_of(day: date) -> list[datetime]:
    return [datetime.combine(day, time(hour), tzinfo=UTC) for hour in MARK_HOURS]


def open_at(exposure: Exposure, mark: datetime) -> bool:
    if _utc(exposure.opened_at) > mark:
        return False
    return exposure.closed_at is None or mark < _utc(exposure.closed_at)


def split(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """``total`` shared pro rata by ``weights``, exactly: the shares add up to ``total``."""
    weight_sum = sum(weights, ZERO)
    with localcontext() as context:
        context.prec = 60
        shares = [
            (total * w / weight_sum).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
            for w in weights
        ]
    remainder = total - sum(shares, ZERO)
    if remainder:
        shares[max(range(len(weights)), key=lambda i: (weights[i], -i))] += remainder
    return shares


def allocate_funding(
    exposures: list[Exposure],
    funding: Mapping[tuple[str, date], Decimal],
) -> FundingAllocation:
    """Allocate each (symbol, day) funding total over the rounds open at that day's marks."""
    by_symbol: dict[str, list[Exposure]] = defaultdict(list)
    for exposure in exposures:
        by_symbol[exposure.symbol].append(exposure)
    result = FundingAllocation()
    for (symbol, day), total in sorted(funding.items()):
        if total == 0:
            continue
        marks = marks_of(day)
        weights: list[tuple[Exposure, Decimal]] = []
        sides_at_mark: dict[datetime, set[str]] = defaultdict(set)
        for exposure in by_symbol.get(symbol, []):
            held = [m for m in marks if open_at(exposure, m)]
            if not held or exposure.entry_notional <= 0:
                continue
            weights.append((exposure, exposure.entry_notional * len(held)))
            for mark in held:
                sides_at_mark[mark].add(exposure.position_side)
        if not weights:
            result.unallocated[(symbol, day)] = total
            continue
        if any({"LONG", "SHORT"} <= sides for sides in sides_at_mark.values()):
            result.hedged_symbol_days.add((symbol, day))
        shares = split(total, [w for _e, w in weights])
        for (exposure, _w), share in zip(weights, shares, strict=True):
            result.by_round_day[(exposure.key, day)] = (
                result.by_round_day.get((exposure.key, day), ZERO) + share
            )
    return result


def days_between(
    start: datetime | None, end: datetime | None, days: set[date]
) -> set[date]:
    """The UTC days of ``days`` that overlap ``[start, end)`` (all of them when a bound is open)."""
    first = _utc(start).date() if start is not None else None
    last = (_utc(end) - timedelta(microseconds=1)).date() if end is not None else None
    return {
        d for d in days if (first is None or d >= first) and (last is None or d <= last)
    }
