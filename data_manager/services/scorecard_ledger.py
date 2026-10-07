"""Ledger conservation of the scorecard over a period (petrosa-data-manager#556). Pure, Decimal throughout.

The scorecard puts a round in the period of its close time, while a ledger books each fill on its own day. For
a period ``[start, end)`` this module builds the *ledger net* of the period from the fills and the exchange
funding and shows that the scorecard reconciles with it **exactly**:

    ledger_net  = sum over the period's fills of (reported pnl - fee)  -  funding cost of the period's days
    total_net   = sum of group net_pnl + the ``unattributed`` group
    total_net - ledger_net = open_entry_fee_delta + exit_fee_boundary_delta + realized_boundary_delta
                             + realized_basis_difference + funding_boundary_delta

every term a named, signed amount in P&L terms (net side minus ledger side):

- ``open_entry_fee_delta``: entry fees booked in the period for rounds not closed in it, minus entry fees booked
  before the period for rounds closed in it (the latter is negative: a round that was opened on an earlier day
  carries an entry fee the period's ledger never saw: net +9.00 against ledger +9.50 is a delta of -0.50);
- ``exit_fee_boundary_delta``: the same for the fees of exit (partial close) fills;
- ``realized_boundary_delta``: FIFO realized P&L of the closed rounds' fills outside the period minus the
  reported realized P&L of the fills the period booked for rounds not closed in it;
- ``realized_basis_difference``: FIFO realized minus the realized P&L the exchange event reported, for the
  period's fills of closed rounds (zero when they agree; the round book matches lots FIFO);
- ``funding_boundary_delta``: funding of the closed rounds on days outside the period, minus the funding the
  period's days allocated to rounds that are not closed in it.

With all positions opened and closed inside the period every delta except the basis difference is zero.
``residual`` must be zero; ``exact`` says so.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from data_manager.services.round_book import OpenRound, UnattributedFill
from data_manager.services.scorecard_funding import (
    FundingAllocation,
    days_between,
)

ZERO = Decimal("0")


def _s(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize() if value != 0 else Decimal(0), "f")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _in(when: datetime, start: datetime | None, end: datetime | None) -> bool:
    when = _utc(when)
    return (start is None or when >= _utc(start)) and (end is None or when < _utc(end))


def unattributed_group(
    fills: list[UnattributedFill],
    start: datetime | None,
    end: datetime | None,
    unallocated_funding_income: Decimal,
) -> dict[str, Any]:
    """The explicit group of everything with no round: fills without a strategy or a usable leg, and funding no
    known round held a position for. ``funding_allocated`` is a cost (positive = paid), as in every group."""
    gross = fees = ZERO
    count = fee_unknown = pnl_missing = undated = 0
    by_reason: dict[str, int] = defaultdict(int)
    for fill in fills:
        if fill.when is None:
            undated += 1
            continue
        if not _in(fill.when, start, end):
            continue
        count += 1
        by_reason[fill.reason] += 1
        fees += fill.fee
        fee_unknown += 0 if fill.fee_known else 1
        if fill.reported_pnl is None:
            pnl_missing += 1
        else:
            gross += fill.reported_pnl
    funding_cost = -unallocated_funding_income
    return {
        "fills": count,
        "by_reason": dict(sorted(by_reason.items())),
        "gross_pnl": _s(gross),
        "fees": _s(fees),
        "funding_allocated": _s(funding_cost),
        "net_pnl": _s(gross - fees - funding_cost),
        "fee_unknown_fills": fee_unknown,
        "fills_without_pnl": pnl_missing,
        "undated_fills_left_out": undated,
    }


def period_ledger(
    *,
    closed: list[Any],
    selected: list[Any],
    open_rounds: list[OpenRound],
    unattributed: list[UnattributedFill],
    start: datetime | None,
    end: datetime | None,
    funding_income: Mapping[tuple[str, date], Decimal] | None,
    allocation: FundingAllocation | None,
) -> dict[str, Any]:
    """The ledger net of the period and its reconciliation with the scorecard.

    ``closed`` are all scored rounds, ``selected`` those closed in the period (their ``round_index`` keys the
    allocation); ``funding_income`` is None when
    the exchange funding is unavailable: the funding terms are then left out and ``funding.status`` says so.
    """
    funding_known = funding_income is not None and allocation is not None
    income = funding_income or {}
    alloc = allocation or FundingAllocation()
    all_days = {day for (_symbol, day) in income}
    period_days = days_between(start, end, all_days)

    life: dict[int, Decimal] = defaultdict(lambda: ZERO)
    in_days: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for (key, day), value in alloc.by_round_day.items():
        life[key] += value
        if day in period_days:
            in_days[key] += value
    selected_keys = {r.round_index for r in selected}

    entry_fee_delta = exit_fee_delta = realized_delta = basis = ZERO
    ledger_rounds = ZERO
    pnl_missing = 0

    def reported(entry: Any) -> Decimal:
        nonlocal pnl_missing
        if entry.reported_pnl is None:
            pnl_missing += 1
            return entry.realized
        return entry.reported_pnl

    for r in selected:  # net side: the closed rounds of the period
        for entry in r.log:
            if _in(entry.when, start, end):
                rep = reported(entry)
                ledger_rounds += rep - entry.fee
                basis += entry.realized - rep
            else:
                realized_delta += entry.realized
                if entry.is_entry:
                    entry_fee_delta -= entry.fee
                else:
                    exit_fee_delta -= entry.fee
    for o in (
        open_rounds
    ):  # ledger side: fills the period booked for rounds that are not closed in it
        for entry in o.log:
            if _in(entry.when, start, end):
                rep = reported(entry)
                ledger_rounds += rep - entry.fee
                realized_delta -= rep
                if entry.is_entry:
                    entry_fee_delta += entry.fee
                else:
                    exit_fee_delta += entry.fee

    unallocated_in = (
        sum(
            (v for (_sym, day), v in alloc.unallocated.items() if day in period_days),
            ZERO,
        )
        if funding_known
        else ZERO
    )
    group = unattributed_group(unattributed, start, end, unallocated_in)
    ledger_unattributed = ZERO
    for fill in unattributed:
        if fill.when is not None and _in(fill.when, start, end):
            ledger_unattributed += (fill.reported_pnl or ZERO) - fill.fee

    allocated_in = sum(
        (v for key, v in in_days.items()), ZERO
    )  # to every round, closed or open
    exchange_in = sum(
        (v for (_sym, day), v in income.items() if day in period_days), ZERO
    )
    selected_life = sum((life[k] for k in selected_keys), ZERO)
    funding_delta = selected_life - allocated_in if funding_known else ZERO

    groups_net = sum((r.net for r in selected), ZERO)
    unattributed_net = Decimal(group["net_pnl"])
    total_net = groups_net + unattributed_net
    ledger_net = ledger_rounds + ledger_unattributed + exchange_in
    difference = total_net - ledger_net
    deltas = {
        "open_entry_fee_delta": entry_fee_delta,
        "exit_fee_boundary_delta": exit_fee_delta,
        "realized_boundary_delta": realized_delta,
        "realized_basis_difference": basis,
        "funding_boundary_delta": funding_delta,
    }
    residual = difference - sum(deltas.values(), ZERO)
    return {
        "basis": "ledger_net = sum of the period's fills (reported pnl - fee) - funding cost of the period's UTC "
        "days; total_net - ledger_net = the named deltas below",
        "ledger_net": _s(ledger_net),
        "groups_net": _s(groups_net),
        "unattributed_net": _s(unattributed_net),
        "total_net": _s(total_net),
        "difference": _s(difference),
        **{name: _s(value) for name, value in deltas.items()},
        "residual": _s(residual),
        "exact": residual == 0,
        "fills_without_pnl_used_fifo": pnl_missing,
        "funding": {
            "status": "allocated" if funding_known else "unavailable",
            "exchange_total_cost": _s(-exchange_in) if funding_known else None,
            "allocated_to_rounds": _s(-allocated_in) if funding_known else None,
            "unallocated": _s(-unallocated_in) if funding_known else None,
            "conserved": (allocated_in + unallocated_in == exchange_in)
            if funding_known
            else None,
        },
        "unattributed_group": group,
    }
