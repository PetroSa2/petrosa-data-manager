"""Per-strategy stats from the exchange ground truth (petrosa-data-manager#842).

Input: the output of :func:`data_manager.services.exchange_episodes.build_ground_truth_v2`, i.e. the account's
flat-to-flat exchange episodes reconciled to the exchange ledger (petrosa-data-manager#844). Output: per strategy,
the two contracts CIO already reads, so CIO needs no change:

- ``performance``: the ``stats`` of ``/analysis/performance/{strategy}`` (wins, losses, win rate, win-rate delta,
  trailing loss streak, realized P&L, trend);
- ``rounds``: the per-strategy entry of ``/analysis/rounds`` (closed rounds, W/L, rate per day, median holding,
  first fill, last close, open rounds).

**Multi-strategy episodes (rule A, the user's decision on #842).** One exchange position is often opened by several
strategies. Each participant gets the episode's P&L in proportion to its share of the opening quantity, and the
episode's outcome (win or loss) counts fully for every participant. So no strategy ever looks better because its
losses sit in a shared position (attributed-only counting hid iceberg_detector's -347 as -22), and no bystander is
charged a loss it did not carry (full-loss counting charged a 1-4 % participant -370).

Edges: episodes still open at the window end, and episodes whose opening fills are before the window, are left out
of W/L and P&L. An episode with an unknown net (a non-USDT fee) counts in ``net_unknown``, never as a win, a loss
or zero. A strategy with no closed episode gets no win rate (``None``): no data is unknown, never zero.
Pure: no I/O.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

RULE = "A: quantity-share P&L, shared outcome counts for every participant"
STATS_SOURCE = "exchange_ground_truth"
_EXCLUDED = ("open_at_window_end", "started_before_window")


def _iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC).isoformat()


def _participants(episode: dict[str, Any]) -> dict[str, float]:
    """Each participating strategy and its share of the episode's opening quantity."""
    if episode.get("status") == "attributed" and episode.get("strategy_id"):
        return {str(episode["strategy_id"]): 1.0}
    shares = episode.get("strategy_shares") or {}
    return {str(s): float(w) for s, w in shares.items() if float(w) > 0}


def _risk_at_entry(episode: dict[str, Any], strategy: str) -> float | None:
    """The strategy's risk at entry in this episode: sum over its rows of qty x entry x stop distance.

    Rows are the strategy position rows linked to the episode by their entry order id (entries are reliable;
    petrosa-tradeengine#838 only affects exits, and #837's stale "open" status doesn't change a row's entry stop).
    Unknown (None) when the strategy has no linked row or any of its rows lacks a usable stop, entry or quantity:
    a partial risk would overstate R.
    """
    from data_manager.services.strategy_net_r import stop_fraction_of

    rows = [
        r for r in episode.get("ledger_rows") or [] if r.get("strategy_id") == strategy
    ]
    if not rows:
        return None
    risk = 0.0
    for row in rows:
        fraction = stop_fraction_of(row.get("entry_price"), row.get("stop_loss_price"))
        try:
            quantity = float(row.get("entry_quantity"))
            entry = float(row.get("entry_price"))
        except (TypeError, ValueError):
            return None
        if fraction is None or quantity <= 0 or entry <= 0:
            return None
        risk += quantity * entry * float(fraction)
    return risk if risk > 0 else None


def build_strategy_stats(
    ground_truth: dict[str, Any], *, window_days: float
) -> dict[str, dict[str, Any]]:
    """Per strategy: ``performance`` and ``rounds`` in CIO's contracts, plus the P&L shares behind them."""
    from data_manager.api.routes.analysis import outcome_stats

    closed: dict[str, list[tuple[int, bool, float, float, dict[str, Any]]]] = {}
    unknown: dict[str, int] = {}
    open_rounds: dict[str, list[int]] = {}
    for episode in ground_truth.get("episodes") or []:
        parts = _participants(episode)
        if not parts:
            continue
        status = episode.get("status")
        if status == "open_at_window_end":
            for strategy in parts:
                open_rounds.setdefault(strategy, []).append(
                    int(episode.get("open_time_ms") or 0)
                )
            continue
        if status in _EXCLUDED or episode.get("close_time_ms") is None:
            continue
        net = episode.get("net_pnl")
        for strategy, share in parts.items():
            if net is None:
                unknown[strategy] = unknown.get(strategy, 0) + 1
                continue
            closed.setdefault(strategy, []).append(
                (
                    int(episode["close_time_ms"]),
                    float(net) > 0,
                    share,
                    float(net),
                    episode,
                )
            )

    stats: dict[str, dict[str, Any]] = {}
    for strategy in sorted(set(closed) | set(unknown) | set(open_rounds)):
        rows = sorted(closed.get(strategy, []), key=lambda r: r[0])
        outcome = outcome_stats([won for _, won, _, _, _ in rows])
        realized = sum(
            share * float(ep.get("realized_pnl") or 0.0) for _, _, share, _, ep in rows
        )
        commission = sum(
            share * float(ep.get("commission") or 0.0) for _, _, share, _, ep in rows
        )
        net = sum(share * value for _, _, share, value, _ in rows)
        holdings = [
            (ep["close_time_ms"] - ep["open_time_ms"]) / 1000.0
            for _, _, _, _, ep in rows
            if ep.get("open_time_ms") is not None
        ]
        fills = sum(len(ep.get("fill_ids") or ()) for _, _, _, _, ep in rows)
        opened = [
            ep.get("open_time_ms") for _, _, _, _, ep in rows if ep.get("open_time_ms")
        ]
        open_at = open_rounds.get(strategy, [])
        stats[strategy] = {
            "performance": {
                "win_rate": outcome["win_rate"],
                "win_rate_delta": outcome["win_rate_delta"],
                "win_rate_delta_window": outcome["win_rate_delta_window"],
                "win_rate_delta_se": outcome["win_rate_delta_se"],
                "wins": outcome["wins"],
                "losses": outcome["losses"],
                "consecutive_losses": outcome["consecutive_losses"],
                "recent_pnl_trend": (
                    "positive" if net > 0 else "negative" if net < 0 else "neutral"
                ),
                "realized_pnl": realized if rows else None,
                "unrealized_pnl": None,
                "net_pnl": net if rows else None,
                "commission": commission if rows else None,
                "net_unknown": unknown.get(strategy, 0),
            },
            "rounds": {
                "fills": fills,
                "closed_rounds": len(rows),
                "closed_rounds_in_window": len(rows),
                "open_rounds": len(open_at),
                "wins": outcome["wins"],
                "losses": outcome["losses"],
                "n": len(rows),
                "window_days": window_days,
                "closed_round_rate_per_day": (len(rows) / window_days)
                if window_days
                else None,
                "median_holding_seconds": statistics.median(holdings)
                if holdings
                else None,
                "holding_source": "median" if holdings else None,
                "first_fill_at": _iso(min(opened)) if opened else None,
                "last_closed_at": _iso(rows[-1][0]) if rows else None,
                "oldest_open_round_opened_at": _iso(min(open_at)) if open_at else None,
                "realized_pnl_closed_rounds": realized if rows else None,
            },
            "shared_episodes": sum(
                1 for *_, ep in rows if ep.get("status") != "attributed"
            ),
            "net_r": _net_r(strategy, rows),
        }
    return stats


def _net_r(
    strategy: str, rows: list[tuple[int, bool, float, float, dict[str, Any]]]
) -> dict[str, Any]:
    """The keep/kill contract (``/analysis/strategy-net-r``) for one strategy, oldest close first.

    ``net_r`` holds only rounds with a known risk; the others are counted in ``rounds_without_risk`` and never
    enter as 0, so a strategy whose rounds mostly lack a stop is "too few rounds" to CIO, never "keep".
    """
    net_r: list[str] = []
    unknown = 0
    total = 0.0
    for _, _, share, net, episode in rows:
        pnl = share * net
        total += pnl
        risk = _risk_at_entry(episode, strategy)
        if risk is None:
            unknown += 1
            continue
        net_r.append(repr(pnl / risk))
    return {
        "net_r": net_r,
        "cumulative_net_loss_usd": repr(max(0.0, -total)),
        "closed_rounds": len(rows),
        "synthetic_rounds": 0,
        "rounds_without_risk": unknown,
        "unknown_r_share": (unknown / len(rows)) if rows else None,
        "fee_unknown_fills": 0,
    }
