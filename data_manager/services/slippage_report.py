"""Slippage per market regime, from the per-fill cost telemetry (petrosa-data-manager#535).

Decision 21 of PetroSa2/petrosa_k8s#1239 assumed twice the slippage on ``turbulent_illiquidity`` pairs. This
measures it: each fill's slippage (basis points against the intended price, positive = adverse, as recorded by
tradeengine's cost telemetry) is joined with the regime in force for its symbol at fill time (the newest
``analytics_<SYMBOL>_regime`` document computed at or before the fill) and grouped by regime and by symbol, with
count, mean, median and p90 and the ratio of each regime's median to the overall median. Reporting only.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean, median
from typing import Any

FILL_EVENT_TYPES = frozenset({"filled", "partial_fill"})
NO_REGIME = "no_regime"
ROLES = ("entry", "exit")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        epoch = float(value)
        return datetime.fromtimestamp(epoch / 1000 if epoch > 1e12 else epoch, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _field(row: dict[str, Any], name: str) -> Any:
    """A telemetry field, whether stored on the event or inside its ``payload``."""
    if row.get(name) is not None:
        return row[name]
    payload = row.get("payload")
    return payload.get(name) if isinstance(payload, dict) else None


def fill_role(row: dict[str, Any]) -> str:
    """``exit`` for a reduce-only fill (stop-loss, take-profit, close), ``entry`` otherwise."""
    role = _field(row, "role")
    if role in ROLES:
        return str(role)
    return "exit" if _field(row, "reduce_only") is True else "entry"


def regime_time(doc: dict[str, Any]) -> datetime | None:
    """When a regime document was computed."""
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    return _when(
        doc.get("computed_at") or metadata.get("computed_at") or doc.get("timestamp")
    )


class RegimeTimeline:
    """The regime in force for one symbol at any time: the newest document computed at or before it."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        dated = [(regime_time(doc), doc) for doc in docs]
        dated = sorted(
            (
                (when, doc)
                for when, doc in dated
                if when is not None and doc.get("regime")
            ),
            key=lambda item: item[0],
        )
        self._times = [when for when, _ in dated]
        self._regimes = [str(doc["regime"]) for _, doc in dated]

    def at(self, when: datetime) -> str:
        index = bisect_right(self._times, when) - 1
        return self._regimes[index] if index >= 0 else NO_REGIME


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile of a non-empty list (``q`` in 0..100)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values: list[float], overall_median: float | None) -> dict[str, Any]:
    group_median = median(values)
    return {
        "count": len(values),
        "mean_bp": mean(values),
        "median_bp": group_median,
        "p90_bp": percentile(values, 90),
        "ratio_to_overall_median": (
            group_median / overall_median if overall_median else None
        ),
    }


def build_report(
    fills: list[dict[str, Any]],
    regimes: dict[str, list[dict[str, Any]]],
    *,
    role: str | None = None,
) -> dict[str, Any]:
    """Slippage per regime and per (regime, symbol).

    ``fills`` are ``execution_events`` rows; ``regimes`` maps a symbol to its regime documents. A fill with
    no regime computed yet is grouped under ``no_regime``; a fill without recorded slippage is counted and left
    out of the statistics. ``role`` keeps only ``entry`` or ``exit`` fills.
    """
    timelines = {symbol: RegimeTimeline(docs) for symbol, docs in regimes.items()}
    by_regime: dict[str, list[float]] = defaultdict(list)
    by_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
    overall: list[float] = []
    without_slippage = 0
    without_time = 0
    considered = 0
    for row in fills:
        if row.get("event_type") not in FILL_EVENT_TYPES:
            continue
        if role and fill_role(row) != role:
            continue
        considered += 1
        slippage = _number(_field(row, "slippage_bp"))
        if slippage is None:
            without_slippage += 1
            continue
        when = _when(row.get("fill_time") or row.get("timestamp"))
        symbol = str(row.get("symbol") or "")
        if when is None or not symbol:
            without_time += 1
            continue
        timeline = timelines.get(symbol)
        regime = timeline.at(when) if timeline else NO_REGIME
        by_regime[regime].append(slippage)
        by_pair[(regime, symbol)].append(slippage)
        overall.append(slippage)

    overall_median = median(overall) if overall else None
    return {
        "role": role or "all",
        "fills_considered": considered,
        "fills_with_slippage": len(overall),
        "fills_without_slippage": without_slippage,
        "fills_without_time_or_symbol": without_time,
        "overall": _stats(overall, overall_median) if overall else None,
        "by_regime": {
            regime: _stats(values, overall_median)
            for regime, values in sorted(by_regime.items())
        },
        "by_regime_and_symbol": {
            f"{regime}/{symbol}": _stats(values, overall_median)
            for (regime, symbol), values in sorted(by_pair.items())
        },
    }


def summary_lines(report: dict[str, Any]) -> list[str]:
    """A short text summary of the report."""
    lines = [
        f"slippage per regime (role={report['role']}): {report['fills_with_slippage']} of "
        f"{report['fills_considered']} fills have slippage"
    ]
    overall = report["overall"]
    if overall:
        lines.append(
            f"overall median {overall['median_bp']:.2f} bp (n={overall['count']})"
        )
    for regime, stats in report["by_regime"].items():
        ratio = stats["ratio_to_overall_median"]
        lines.append(
            f"{regime}: n={stats['count']} mean={stats['mean_bp']:.2f} median={stats['median_bp']:.2f} "
            f"p90={stats['p90_bp']:.2f} bp, ratio to overall median "
            f"{'-' if ratio is None else f'{ratio:.2f}x'}"
        )
    return lines
