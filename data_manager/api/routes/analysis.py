"""
Analytics endpoints for computed metrics.
"""

import asyncio
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic
from typing import Any

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, HTTPException, Query
from prometheus_client import Gauge
from pydantic import BaseModel

import constants
import data_manager.api.app as api_module
from data_manager.services.report_precompute import record_report_stage

logger = logging.getLogger(__name__)

_SLIPPAGE_MAX_FILLS = 50_000
_REGIME_MAX_DOCS = 20_000
_ROUND_MAX_FILLS = 50_000
ROUND_BOOK_ORPHANED_LOTS = Gauge(
    "data_manager_round_book_orphaned_lots", "Orphaned round-book lots", ["strategy_id"]
)
ROUND_BOOK_SNAPSHOT_AGE = Gauge(
    "data_manager_round_book_exchange_snapshot_age_seconds",
    "Age of the exchange snapshot used by the round-book overlay",
)
ROUND_BOOK_ORPHAN_OVERLAY_UNMATCHED_QUANTITY = Gauge(
    "data_manager_round_book_orphan_overlay_unmatched_quantity",
    "Cached orphan quantity without a matching live lot",
    ["strategy_id"],
)

_REPLAY_SECONDS_PER_FILL = 0.004
_CIO_TIMEOUT_SECONDS = 10.0
_REPLAY_WARNING_FILL_COUNT = int(_CIO_TIMEOUT_SECONDS / _REPLAY_SECONDS_PER_FILL)

#: Fields ``slippage_report.build_report`` reads from an ``execution_events`` fill: the event type, symbol and
#: times, and the telemetry (``role``, ``reduce_only``, ``slippage_bp``, ``intended_price``, ``regime_at_fill``)
#: that may sit on the event or inside its ``payload``. The golden test applies this projection to
#: production-shaped documents and requires an identical report.
SLIPPAGE_FILL_PROJECTION: dict[str, int] = {
    "event_type": 1,
    "symbol": 1,
    "timestamp": 1,
    "fill_time": 1,
    "role": 1,
    "reduce_only": 1,
    "slippage_bp": 1,
    "intended_price": 1,
    "regime_at_fill": 1,
    "payload.role": 1,
    "payload.reduce_only": 1,
    "payload.slippage_bp": 1,
    "payload.intended_price": 1,
    "payload.regime_at_fill": 1,
}
#: Fields ``slippage_report.regime_time`` and ``RegimeTimeline`` read from an ``analytics_<pair>_regime`` doc.
REGIME_DOC_PROJECTION: dict[str, int] = {
    "regime": 1,
    "computed_at": 1,
    "metadata.computed_at": 1,
    "timestamp": 1,
}
# The round book (``compute_closed_rounds``) and the risk-inputs fills and candles are read WITHOUT a
# projection on purpose: the round book reads many event and payload fields (fees, position ids, sides,
# reasons) and a projection there would need its own golden test first.

router = APIRouter()
calibration_router = APIRouter()


async def compute_calibration_confidence(
    db_manager: Any,
    since: datetime | None = None,
    strategy_id: str | None = None,
    source: str = "on_demand",
) -> dict[str, Any]:
    """Compute the confidence calibration report without route or cache concerns."""
    from data_manager.services.calibration_service import get_calibration_records

    if not db_manager or not getattr(db_manager, "mongodb_adapter", None):
        raise HTTPException(status_code=503, detail="Database not available")
    return await get_calibration_records(
        db_manager.mongodb_adapter, since=since, strategy_id=strategy_id, source=source
    )


@router.get("/calibration/confidence")
async def get_calibration_confidence(
    since: datetime | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    """Return closed, CIO-executed outcomes paired with point-in-time confidence."""
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        precomputer = getattr(api_module, "report_precomputer", None)
        if precomputer is not None and since is None and strategy_id is None:
            cached = await precomputer.get_or_compute(
                "calibration",
                lambda: compute_calibration_confidence(api_module.db_manager),
            )
            if cached is not None:
                return cached
            raise HTTPException(
                status_code=503,
                detail="report_warming",
                headers={"Retry-After": "15"},
            )
        return await compute_calibration_confidence(
            api_module.db_manager, since=since, strategy_id=strategy_id
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("confidence calibration failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503, detail="Calibration data unavailable"
        ) from exc


@calibration_router.get("/calibration/latest")
async def get_latest_calibration_report(
    since: datetime | None = Query(None),
    strategy_id: str | None = Query(None),
) -> dict[str, Any]:
    """Return the calibration report used by production callers and its freshness."""
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="Database not available")
    from data_manager.services.calibration_service import get_latest_calibration

    try:
        return await get_latest_calibration(
            api_module.db_manager.mongodb_adapter,
            since=since,
            strategy_id=strategy_id,
        )
    except Exception as exc:
        logger.error("latest confidence calibration failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503, detail="Calibration data unavailable"
        ) from exc


class MetricResponse(BaseModel):
    """Generic metric response."""

    pair: str
    period: str
    metric: str
    method: str
    window: str
    values: list[dict]
    metadata: dict


@router.get("/volatility")
async def get_volatility(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    method: str = Query("rolling_stddev", description="Volatility calculation method"),
    window: str = Query("30d", description="Time window for calculation"),
) -> MetricResponse:
    """
    Get volatility metrics for a trading pair.

    Supported methods: rolling_stddev, annualized, parkinson, garman_klass
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        # Query from MongoDB analytics collection
        collection = f"analytics_{pair}_volatility"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=10
        )

        # Format as time series
        values = [
            {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "rolling_stddev": str(r.get("rolling_stddev", "0")),
                "annualized": str(r.get("annualized_volatility", "0")),
                "parkinson": str(r.get("parkinson")) if r.get("parkinson") else None,
                "garman_klass": str(r.get("garman_klass"))
                if r.get("garman_klass")
                else None,
                "vov": (
                    str(r.get("volatility_of_volatility"))
                    if r.get("volatility_of_volatility")
                    else None
                ),
            }
            for r in results
        ]

        return MetricResponse(
            pair=pair,
            period=period,
            metric="volatility",
            method=method,
            window=window,
            values=values,
            metadata={
                "data_completeness": 100.0,
                "last_updated": datetime.now(UTC).isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching volatility metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _unavailable_exposure(status: str) -> dict[str, Any]:
    return {"status": status, "entries": None}


def _normalise_overlay_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("overlay timestamp is not a datetime or ISO string")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


async def _orphaned_lots_from_the_cache(strategy_id: str) -> dict[str, Any]:
    """The orphaned lots of a strategy per the round-book overlay (#577), from the precomputed rounds report.

    The cache only says WHICH lots are orphaned (symbol, leg, order id, time, quantity); the amounts are
    valued from the live replay of the route's own fills (``_leg_aware_pnl``), so cache lag cannot double count
    a lot sold since or miss one opened since. CACHE READ ONLY: it never computes the report (a cold compute
    reads every fill and would run inline on the CIO's decision path): a miss is ``warming``, no precompute is
    ``unavailable``. A cached report older than the exchange snapshot allows, or of unknown age, is
    ``disabled_stale``: the overlay was decided when the report was computed, and
    ``ROUND_ORPHAN_MAX_SNAPSHOT_AGE_SECONDS`` bounds the age of the snapshot plus the age of the cache. In every
    such case (and for a malformed body) nothing is excluded.
    """
    from data_manager.services.round_book import ROUND_ORPHAN_MAX_SNAPSHOT_AGE_SECONDS

    precomputer = getattr(api_module, "report_precomputer", None)
    if precomputer is None:
        return _unavailable_exposure("unavailable")
    try:
        report = await precomputer.get("rounds", window_days=30)
    except Exception as exc:
        logger.warning("performance: rounds cache unreadable: %s", exc)
        return _unavailable_exposure("unavailable")
    if report is None:
        return _unavailable_exposure("warming")
    try:
        status = report.get("orphan_overlay")
        if status != "enabled":
            return _unavailable_exposure(str(status or "unavailable"))
        metadata = report.get("metadata") or {}
        if metadata.get("stale") is not False:  # stale, or unknown
            return _unavailable_exposure("disabled_stale")
        cache_age = float(metadata["age_seconds"])
        snapshot_age = float(report["exchange_snapshot_age_seconds"])
        if cache_age + snapshot_age > ROUND_ORPHAN_MAX_SNAPSHOT_AGE_SECONDS:
            return _unavailable_exposure("disabled_stale")
        strategies = report.get("strategies") or {}
        if strategies.get(strategy_id) is None:
            # the report predates this strategy's first fill: nothing is known about its lots
            return _unavailable_exposure("unavailable")
        entries = [
            (
                str(symbol),
                str(leg),
                _normalise_overlay_timestamp(lot["opened_at"]),
                lot["order_id"],
                float(lot["quantity"]),
                kind,
            )
            for symbol, by_leg in (strategies[strategy_id].get("legs") or {}).items()
            for leg, data in by_leg.items()
            for kind in ("orphaned", "ledger_closed")
            for lot in (data["orphaned"] if kind == "orphaned" else data.get("ledger_closed", []))
        ]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("performance: rounds report not usable: %s", exc)
        return _unavailable_exposure("unavailable")
    return {"status": "enabled", "entries": entries}


def _leg_aware_pnl(
    rows: list[dict[str, Any]],
    overlay_lots: list[tuple[str, str, str, Any, float, str]],
    strategy_id: str,
) -> dict[str, Any] | None:
    """Realized and unrealized P&L of one strategy from ONE leg-aware replay of its own fills (#581).

    The replay is the round book's: hedge legs stay apart, so realized and unrealized describe the same lots at
    the same time (``PnlCalculator`` nets BUY against SELL on a symbol whatever the position side). The cached
    overlay list is matched to the still-open lots by identity (symbol, leg, order id, opened at); a lot closed
    since the cache was computed is simply not open here, and a lot opened since is held by default. Returns
    ``None`` when an open lot has no mark price (nothing is guessed).
    """
    from data_manager.services.round_book import (
        FILL_EVENT_TYPES,
        ROUND_ORPHAN_QUANTITY_TOLERANCE,
        RoundBook,
        _number,
        _when,
    )

    book = RoundBook()
    marks: dict[str, float] = {}
    fills = [row for row in rows if row.get("event_type") in FILL_EVENT_TYPES]
    for row in sorted(
        fills, key=lambda r: _when(r) or datetime.min.replace(tzinfo=UTC)
    ):
        book.apply(row)
        symbol = row.get("symbol")
        price = _number(row.get("fill_price") or row.get("price"))
        quantity = _number(
            row.get("fill_qty") or row.get("fill_quantity") or row.get("qty")
        )
        if symbol and price and price > 0 and quantity and quantity > 0:
            marks[str(symbol)] = (
                price  # the latest fill price, in the price the lots are valued at
            )
    excluded: dict[tuple[str, str, Any, str], dict[str, float]] = {}
    for symbol, leg, opened_at, order_id, quantity, kind in overlay_lots:
        key = (symbol, leg, order_id, opened_at)
        by_kind = excluded.setdefault(key, {})
        by_kind[kind] = by_kind.get(kind, 0.0) + quantity
    held_pnl = 0.0
    orphaned_pnl = 0.0
    orphaned_lots = 0
    ledger_closed_pnl = 0.0
    ledger_closed_lots = 0
    for symbol, leg, lot in book.open_lots(strategy_id):
        mark = marks.get(symbol)
        if mark is None:
            return None
        key = (symbol, leg, lot.order_id, _normalise_overlay_timestamp(lot.opened_at))
        by_kind = excluded.get(key, {})
        orphaned_taken = min(lot.qty, by_kind.get("orphaned", 0.0))
        remaining = lot.qty - orphaned_taken
        ledger_closed_taken = min(remaining, by_kind.get("ledger_closed", 0.0))
        remaining -= ledger_closed_taken
        if "orphaned" in by_kind:
            by_kind["orphaned"] -= orphaned_taken
        if "ledger_closed" in by_kind:
            by_kind["ledger_closed"] -= ledger_closed_taken
        sign = 1.0 if leg == "LONG" else -1.0
        per_unit = sign * (mark - lot.price)
        held_pnl += per_unit * remaining
        orphaned_pnl += per_unit * orphaned_taken
        ledger_closed_pnl += per_unit * ledger_closed_taken
        if orphaned_taken > ROUND_ORPHAN_QUANTITY_TOLERANCE * max(1.0, lot.qty):
            orphaned_lots += 1
        if ledger_closed_taken > ROUND_ORPHAN_QUANTITY_TOLERANCE * max(1.0, lot.qty):
            ledger_closed_lots += 1
    unmatched_quantity = sum(
        quantity
        for by_kind in excluded.values()
        for kind, quantity in by_kind.items()
        if kind == "orphaned" and quantity > ROUND_ORPHAN_QUANTITY_TOLERANCE
    )
    unmatched_lots = sum(
        quantity > ROUND_ORPHAN_QUANTITY_TOLERANCE
        for by_kind in excluded.values()
        for kind, quantity in by_kind.items()
        if kind == "orphaned"
    )
    return {
        "realized": book.realized_total(strategy_id),
        "held_unrealized": held_pnl,
        "orphaned_unrealized": orphaned_pnl,
        "orphaned_lots": orphaned_lots,
        "ledger_closed_unrealized": ledger_closed_pnl,
        "ledger_closed_lots": ledger_closed_lots,
        "unmatched_quantity": unmatched_quantity,
        "unmatched_lots": unmatched_lots,
    }


def win_rate_delta_with_noise_floor(
    outcomes: list[bool],
) -> tuple[float | None, int | None, float | None]:
    """The win rate of the latest window minus the one before it, only when it is distinguishable from noise.

    The two windows hold ``w = n // 2`` outcomes each, ``a`` wins in the earlier and ``b`` in the later one.
    With the pooled win rate ``p = (a + b) / 2w`` the standard error of the difference is
    ``SE = sqrt(p (1 - p) 2 / w)``, and the delta ``(b - a) / w`` is returned only when ``|delta| > 2 SE``
    (about 95 %), else ``None`` (unknown to the CIO). The test is exact, in integers, so a tie is never decided
    by rounding: ``|delta| > 2 SE`` is ``(b - a)^2 w > 2 (a + b) (2w - a - b)``. A pooled ``p`` of 0 or 1 has no
    variance (and no delta), so ``SE`` is reported from the Laplace estimate ``(a + b + 1) / (2 w + 2)``
    there. No fixed sample size is assumed: with three outcomes each window is one trade, the delta can only be
    -1, 0 or +1 and is never above 2 SE (petrosa-data-manager#579). Returns ``(delta, w, SE)``;
    ``(None, None, None)`` below two outcomes.
    """
    window = len(outcomes) // 2
    if not window:
        return None, None, None
    earlier = sum(outcomes[-2 * window : -window])
    later = sum(outcomes[-window:])
    wins = earlier + later
    if wins in (0, 2 * window):
        pooled = (wins + 1) / (2 * window + 2)
        return None, window, math.sqrt(pooled * (1 - pooled) * 2 / window)
    standard_error = math.sqrt(wins * (2 * window - wins) / (2 * window**3))
    significant = (later - earlier) ** 2 * window > 2 * wins * (2 * window - wins)
    return ((later - earlier) / window if significant else None), window, standard_error


@router.get("/performance/{strategy_id}")
async def get_strategy_performance(strategy_id: str):
    """
    Returns historical performance metrics for a specific strategy.

    Computes a real win-rate + recent-P&L trend by replaying the
    persisted `execution_events` fills through the FIFO P&L calculator
    (P4.1, #601). When the database is unavailable or no fills exist,
    the response degrades to a "no-data" payload rather than failing
    the request.
    """
    try:
        import data_manager.api.app as api_module
        from data_manager.services.pnl_calculator import PnlCalculator

        if not api_module.db_manager or not getattr(
            api_module.db_manager, "mongodb_adapter", None
        ):
            return {
                "stats": {
                    "win_rate": None,
                    "win_rate_delta": None,
                    "win_rate_delta_window": None,
                    "win_rate_delta_se": None,
                    "consecutive_losses": None,
                    # "neutral" (not "unknown") — matches petrosa-cio's PnlTrend
                    # enum vocabulary (positive|negative|neutral). Same bug
                    # class as the zero-total "flat" fix in #306/#309; this
                    # sentinel path was explicitly out of that PR's AC and is
                    # closed by PetroSa2/petrosa-cio#194.
                    "recent_pnl_trend": "neutral",
                },
                "metadata": {
                    "strategy_id": strategy_id,
                    "calculated_at": datetime.now(UTC).isoformat(),
                    "source": "data-manager-analysis-no-db",
                },
            }

        mongodb = api_module.db_manager.mongodb_adapter
        try:
            cursor = (
                mongodb.db["execution_events"]
                .find(
                    {
                        "strategy_id": strategy_id,
                        "event_type": {"$in": ["filled", "partial_fill"]},
                    }
                )
                .sort("timestamp", 1)
            )
            rows = await cursor.to_list(length=None)
        except Exception as exc:
            # A misconfigured / mock adapter (or a transient MongoDB
            # error) degrades to the no-data payload rather than 500ing.
            # Real errors still get logged for ops to investigate.
            logger.warning(
                "performance: execution_events read failed for %s: %s",
                strategy_id,
                exc,
            )
            return {
                "stats": {
                    "win_rate": None,
                    "win_rate_delta": None,
                    "win_rate_delta_window": None,
                    "win_rate_delta_se": None,
                    "consecutive_losses": None,
                    # See comment on the no-DB sentinel above: "neutral", not
                    # "unknown" — cio#194 sibling fix.
                    "recent_pnl_trend": "neutral",
                },
                "metadata": {
                    "strategy_id": strategy_id,
                    "calculated_at": datetime.now(UTC).isoformat(),
                    "source": "data-manager-analysis-no-db",
                },
            }

        calc = PnlCalculator()
        wins = 0
        losses = 0
        outcomes: list[bool] = []
        for row in rows:
            impact = calc.apply_fill(row)
            if impact is None or impact.realized_pnl == 0:
                continue
            if impact.realized_pnl > 0:
                wins += 1
                outcomes.append(True)
            else:
                losses += 1
                outcomes.append(False)

        decisions = wins + losses
        win_rate = (wins / decisions) if decisions else None
        win_rate_delta, delta_window, delta_se = win_rate_delta_with_noise_floor(
            outcomes
        )

        consecutive_losses = None
        if outcomes:
            consecutive_losses = 0
            for won in reversed(outcomes):
                if won:
                    break
                consecutive_losses += 1

        breakdown = calc.strategy_pnl(strategy_id)
        # The orphan overlay of #577. off = unchanged. report = the orphaned amounts only (PnlCalculator figures
        # stay). apply = realized and unrealized both come from ONE leg-aware replay of these fills with the
        # orphaned lots left out of unrealized, so the trend total describes one book at one time.
        orphan_mode = _round_overlay_mode()
        exposure = None
        leg_pnl = None
        if orphan_mode != "off":
            exposure = await _orphaned_lots_from_the_cache(strategy_id)
            if exposure["entries"] is not None:
                if len(rows) > _REPLAY_WARNING_FILL_COUNT:
                    logger.warning(
                        "performance: replay has %s fills; warning threshold is %s",
                        len(rows),
                        _REPLAY_WARNING_FILL_COUNT,
                    )
                leg_pnl = await asyncio.to_thread(
                    _leg_aware_pnl, rows, exposure["entries"], strategy_id
                )
                if leg_pnl is None:
                    exposure = _unavailable_exposure("unavailable")
        ROUND_BOOK_ORPHAN_OVERLAY_UNMATCHED_QUANTITY.labels(
            strategy_id=strategy_id
        ).set(leg_pnl["unmatched_quantity"] if leg_pnl else 0)
        realized_pnl = breakdown.realized
        unrealized_pnl = breakdown.unrealized
        if orphan_mode == "apply" and leg_pnl is not None:
            realized_pnl = leg_pnl["realized"]
            unrealized_pnl = leg_pnl["held_unrealized"]
        total_pnl = realized_pnl + unrealized_pnl
        recent_trend = (
            "positive"
            if total_pnl > 0
            else "negative"
            if total_pnl < 0
            # "neutral" (not "flat") — matches petrosa-cio's PnlTrend enum vocabulary
            # (positive|negative|neutral). See PetroSa2/petrosa-data-manager#306.
            else "neutral"
        )

        return {
            "stats": {
                "win_rate": win_rate,
                "win_rate_delta": win_rate_delta,
                # The windows and the standard error behind the delta (and why it may be null)
                "win_rate_delta_window": delta_window,
                "win_rate_delta_se": delta_se,
                # The closed rounds behind the win rate: the posterior of the CIO net-EV gate needs them
                "wins": wins,
                "losses": losses,
                "consecutive_losses": consecutive_losses,
                "recent_pnl_trend": recent_trend,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                **(
                    {
                        "orphaned_unrealized_pnl": (
                            leg_pnl["orphaned_unrealized"] if leg_pnl else None
                        ),
                        "orphaned_lots": leg_pnl["orphaned_lots"] if leg_pnl else None,
                        "ledger_closed_unrealized_pnl": (
                            leg_pnl["ledger_closed_unrealized"] if leg_pnl else None
                        ),
                        "ledger_closed_lots": (
                            leg_pnl["ledger_closed_lots"] if leg_pnl else None
                        ),
                    }
                    if exposure is not None
                    else {}
                ),
            },
            "metadata": {
                "strategy_id": strategy_id,
                "calculated_at": datetime.now(UTC).isoformat(),
                "source": "data-manager-pnl-calculator",
                "fills_replayed": len(rows),
                "legacy_exit_side_mapped": calc.legacy_exit_side_mapped,
                **(
                    {
                        "orphan_overlay": exposure["status"],
                        "orphan_marking": orphan_mode,
                        # which book the realized/unrealized/trend figures come from
                        "pnl_source": (
                            "round-book-legs"
                            if orphan_mode == "apply" and leg_pnl is not None
                            else "pnl-calculator"
                        ),
                        "orphan_overlay_unmatched_quantity": (
                            leg_pnl["unmatched_quantity"] if leg_pnl else None
                        ),
                        "orphan_overlay_unmatched_lots": (
                            leg_pnl["unmatched_lots"] if leg_pnl else None
                        ),
                    }
                    if exposure is not None
                    else {}
                ),
            },
        }
    except Exception as e:
        logger.error(
            f"Error getting strategy performance for {strategy_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scorecard")
async def get_scorecard(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    group_by: str = Query("strategy"),
    minimum_trades: int = Query(30, ge=0),
) -> dict:
    """Return a Decimal-string scorecard from immutable audit collections."""
    if group_by not in {"strategy", "strategy_symbol", "cio_mode"}:
        raise HTTPException(
            status_code=422,
            detail="group_by must be strategy, strategy_symbol, or cio_mode",
        )
    if from_ and to and from_ >= to:
        raise HTTPException(status_code=422, detail="from must be before to")
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="Database not available")
    from data_manager.services.scorecard_service import ScorecardService

    try:
        return await ScorecardService(api_module.db_manager.mongodb_adapter).calculate(
            start=from_, end=to, group_by=group_by, minimum_trades=minimum_trades
        )
    except Exception as exc:
        logger.error("scorecard calculation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503, detail="Scorecard data unavailable"
        ) from exc


@router.get("/scorecard/evaluate")
async def evaluate_scorecard(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    minimum_trades: int | None = Query(None, ge=0),
) -> dict:
    """Report scorecard policy outcomes without changing strategy state."""
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        raise HTTPException(status_code=503, detail="Database not available")
    config = None
    if getattr(api_module.db_manager, "configuration", None):
        config = await api_module.db_manager.configuration.get_app_config()
    parameters = (config or {}).get("parameters", {})
    configured = all(
        parameters.get(name) is not None
        for name in (
            "scorecard_min_trades",
            "scorecard_min_expectancy_net",
            "scorecard_max_dd_fraction",
        )
    )
    if not configured:
        return {"status": "unconfigured", "groups": [], "writes": 0}
    minimum = (
        minimum_trades
        if minimum_trades is not None
        else int(parameters["scorecard_min_trades"])
    )
    scorecard = await get_scorecard(from_, to, "strategy", minimum)
    evaluated = []
    for group in scorecard["groups"]:
        if not group["sample_ok"]:
            status = "watch"
        elif Decimal(str(group["expectancy_per_trade"] or "0")) < Decimal(
            str(parameters["scorecard_min_expectancy_net"])
        ):
            status = "disable"
        elif Decimal(str(group["max_drawdown"] or "0")) > Decimal(
            str(parameters["scorecard_max_dd_fraction"])
        ):
            status = "disable"
        else:
            status = "keep"
        evaluated.append(
            {"strategy_id": group["group"], "status": status, "metrics": group}
        )
    return {
        "status": "configured",
        "thresholds": parameters,
        "groups": evaluated,
        "writes": 0,
    }


async def compute_slippage_by_regime(
    db_manager: Any,
    window_days: float = 30.0,
    symbol: str | None = None,
    role: str | None = None,
    source: str = "on_demand",
) -> dict[str, Any]:
    """Slippage per market regime from the per-fill cost telemetry (petrosa-data-manager#535).

    Joins each fill's slippage with the regime in force for its symbol at fill time and reports the count, mean,
    median and p90 in basis points per regime and per (regime, symbol), with the ratio to the overall median.
    Read-only; fills without recorded slippage are counted and left out, fills before the first regime are
    grouped under ``no_regime``.
    """
    from data_manager.services.slippage_report import FILL_EVENT_TYPES, build_report

    if not db_manager or not getattr(db_manager, "mongodb_adapter", None):
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    since = datetime.now(UTC) - timedelta(days=window_days)
    query: dict[str, Any] = {
        "event_type": {"$in": sorted(FILL_EVENT_TYPES)},
        "timestamp": {"$gte": since},
    }
    if symbol:
        query["symbol"] = symbol
    mongodb = db_manager.mongodb_adapter
    read_started = monotonic()
    try:
        fills = (
            await mongodb.db["execution_events"]
            .find(query, SLIPPAGE_FILL_PROJECTION)
            .sort("timestamp", 1)
            .to_list(length=_SLIPPAGE_MAX_FILLS)
        )
        regimes: dict[str, list[dict[str, Any]]] = {}
        from data_manager.services.slippage_report import _when

        unstamped_oldest: dict[str, datetime] = {}
        for row in fills:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("regime_at_fill") or row.get("regime_at_fill"):
                continue
            pair = str(row.get("symbol") or "")
            fill_when = _when(row.get("fill_time") or row.get("timestamp"))
            if pair and fill_when is not None:
                previous = unstamped_oldest.get(pair)
                if previous is None or fill_when < previous:
                    unstamped_oldest[pair] = fill_when

        for pair in sorted(unstamped_oldest):
            regime_since = unstamped_oldest[pair] - timedelta(
                seconds=constants.ANALYTICS_INTERVAL
            )
            collection = mongodb.db[f"analytics_{pair}_regime"]
            anchor = (
                await collection.find(
                    {"timestamp": {"$lt": regime_since}}, REGIME_DOC_PROJECTION
                )
                .sort("timestamp", -1)
                .to_list(length=1)
            )
            bounded = await collection.find(
                {"timestamp": {"$gte": regime_since}}, REGIME_DOC_PROJECTION
            ).to_list(length=_REGIME_MAX_DOCS)
            regimes[pair] = anchor + bounded
        record_report_stage(
            "slippage_by_regime",
            "read+decode",
            monotonic() - read_started,
            source=source,
        )
        fills = list(fills)
        regimes = {pair: list(rows) for pair, rows in regimes.items()}
    except Exception as exc:
        logger.error("slippage-by-regime: read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    compute_started = monotonic()
    report = await asyncio.to_thread(build_report, fills, regimes, role=role)
    record_report_stage(
        "slippage_by_regime", "compute", monotonic() - compute_started, source=source
    )
    report["metadata"] = {
        "calculated_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "fills_read": len(fills),
        "truncated": len(fills) >= _SLIPPAGE_MAX_FILLS,
        "source": "data-manager-slippage-by-regime",
    }
    return report


@router.get("/slippage-by-regime")
async def get_slippage_by_regime(
    window_days: float = Query(
        30.0, gt=0, le=365, description="Trailing window of fills"
    ),
    symbol: str | None = Query(None, description="One symbol; all when omitted"),
    role: str | None = Query(
        None, pattern="^(entry|exit)$", description="entry or exit fills only"
    ),
):
    precomputer = getattr(api_module, "report_precomputer", None)
    if (
        precomputer is not None
        and symbol is None
        and role is None
        and window_days == 30
    ):
        cached = await precomputer.get_or_compute(
            "slippage_by_regime",
            lambda: compute_slippage_by_regime(api_module.db_manager, 30),
            window_days=30,
        )
        if cached is not None:
            return cached
        raise HTTPException(
            status_code=503,
            detail="report_warming",
            headers={"Retry-After": "15"},
        )
    return await compute_slippage_by_regime(
        api_module.db_manager, window_days, symbol, role
    )


def _round_overlay_mode() -> str:
    mode = os.getenv("ROUND_ORPHAN_MARKING", "report").lower()
    return mode if mode in {"off", "report", "apply"} else "report"


def _slice_round_report(report: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    """One strategy's part of a full replay, with the totals and the check of that strategy alone."""
    strategy = report["strategies"].get(strategy_id)
    report["strategies"] = {strategy_id: strategy} if strategy else {}
    fills = strategy["fills"] if strategy else 0
    report["unattributed"] = {}
    report["totals"] = {
        "fills": fills,
        "attributed_to_a_strategy": fills,
        "unattributed": 0,
        "position_side_unknown": strategy["position_side_unknown"] if strategy else 0,
        "legacy_exit_side_mapped": strategy["legacy_exit_side_mapped"]
        if strategy
        else 0,
    }
    report["accounted"] = strategy is None or strategy["fills"] == strategy[
        "fills_in_closed_rounds"
    ] + strategy["fills_in_open_rounds"] + strategy.get("fills_in_orphaned_rounds", 0)
    return report


async def compute_closed_rounds(
    db_manager: Any,
    strategy_id: str | None = None,
    window_days: float = 30.0,
    source: str = "on_demand",
) -> dict[str, Any]:
    """Per-strategy fills, closed and open rounds, closed-round rate and median holding time.

    Every fill is accounted for: in a closed round, in the open round of its strategy, or unattributed with a
    reason (petrosa-data-manager#537). ``n`` is the number of closed rounds behind the rate and the holding
    time, so a consumer can tell when a figure is too thin to use.

    The orphan overlay (petrosa-data-manager#576) allocates the exchange quantity across ALL strategies, so it
    is built from the replay of every fill. A call for one strategy takes its figures from that replay when it
    is complete; when the read of all fills is cut at the cap, the strategy's figures come from a read of its own
    fills (never from the cut data) and the overlay is reported ``disabled_truncated``.
    """
    from data_manager.services.round_book import FILL_EVENT_TYPES, build_report

    if not db_manager or not getattr(db_manager, "mongodb_adapter", None):
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    mode = _round_overlay_mode()
    mongodb = db_manager.mongodb_adapter
    base_query: dict[str, Any] = {"event_type": {"$in": sorted(FILL_EVENT_TYPES)}}

    async def read(query: dict[str, Any]) -> list[dict[str, Any]]:
        read_started = monotonic()
        cursor = mongodb.db["execution_events"].find(query).sort("timestamp", 1)
        rows = await cursor.to_list(length=_ROUND_MAX_FILLS)
        record_report_stage(
            "rounds", "read+decode", monotonic() - read_started, source=source
        )
        return rows

    own_query = {**base_query, "strategy_id": strategy_id} if strategy_id else None
    try:
        rows = await read(own_query if own_query and mode == "off" else base_query)
        truncated = len(rows) >= _ROUND_MAX_FILLS
        overlay_cut = False
        if own_query and mode != "off" and truncated:
            # The read of all fills is cut: this strategy's figures must not come from it.
            overlay_cut = True
            rows = await read(own_query)
            truncated = len(rows) >= _ROUND_MAX_FILLS
    except Exception as exc:
        logger.error("rounds: execution_events read failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    overlay_possible = mode != "off" and not overlay_cut and not truncated
    exchange = None
    closed_entry_orders: set[str] | None = None
    if overlay_possible:
        from data_manager.db.repositories.ledger_repository import LedgerRepository

        mysql = getattr(db_manager, "mysql_adapter", None)
        if mysql is not None:
            repo = LedgerRepository(mysql, None)
            try:
                exchange, closed_entry_orders = await asyncio.gather(
                    asyncio.to_thread(repo.round_overlay_snapshot),
                    asyncio.to_thread(repo.closed_entry_order_ids),
                )
            except Exception as exc:
                logger.warning("rounds: orphan overlay inputs unavailable: %s", exc)
                exchange, closed_entry_orders = {"as_of_ms": None, "rows": []}, set()
        else:
            exchange, closed_entry_orders = {"as_of_ms": None, "rows": []}, set()
    compute_started = monotonic()
    report = await asyncio.to_thread(
        build_report,
        rows,
        window_days=window_days,
        exchange=exchange,
        closed_entry_orders=closed_entry_orders,
        apply_overlay=mode == "apply",
    )
    record_report_stage(
        "rounds", "compute", monotonic() - compute_started, source=source
    )
    if mode == "off":
        report["orphan_overlay"] = "off"
    elif overlay_cut or truncated:
        report["orphan_overlay"] = "disabled_truncated"
    if strategy_id and not (mode == "off" or overlay_cut):
        report = _slice_round_report(report, strategy_id)
    for owner, strategy in report["strategies"].items():
        ROUND_BOOK_ORPHANED_LOTS.labels(strategy_id=owner).set(
            strategy.get("orphaned_lots", 0)
        )
    if strategy_id and strategy_id not in report["strategies"]:
        ROUND_BOOK_ORPHANED_LOTS.labels(strategy_id=strategy_id).set(0)
    ROUND_BOOK_SNAPSHOT_AGE.set(report.get("exchange_snapshot_age_seconds", -1))
    report["metadata"] = {
        "calculated_at": datetime.now(UTC).isoformat(),
        "fills_read": len(rows),
        "truncated": truncated,
        "source": "data-manager-round-book",
    }
    return report


@router.get("/rounds")
async def get_closed_rounds(
    strategy_id: str | None = Query(None, description="One strategy; all when omitted"),
    window_days: float = Query(
        30.0, gt=0, le=365, description="Window of the rate and holding time"
    ),
):
    precomputer = getattr(api_module, "report_precomputer", None)
    if precomputer is not None and strategy_id is None and window_days == 30:
        cached = await precomputer.get_or_compute(
            "rounds",
            lambda: compute_closed_rounds(api_module.db_manager, None, 30),
            window_days=30,
        )
        if cached is not None:
            return cached
        raise HTTPException(
            status_code=503,
            detail="report_warming",
            headers={"Retry-After": "15"},
        )
    return await compute_closed_rounds(api_module.db_manager, strategy_id, window_days)


@router.get("/rounds/orphaned")
async def get_orphaned_rounds(
    strategy_id: str | None = Query(None),
    window_days: float = Query(30.0, gt=0, le=365),
) -> dict[str, Any]:
    """List read-only orphan and ledger-closed lots from the complete fill replay."""
    report = await compute_closed_rounds(
        api_module.db_manager, strategy_id, window_days
    )
    lots = []
    for owner, strategy in report["strategies"].items():
        for symbol, symbol_legs in strategy.get("legs", {}).items():
            for leg, data in symbol_legs.items():
                for lot in data.get("orphaned", []):
                    lots.append(
                        {"strategy_id": owner, "symbol": symbol, "leg": leg, **lot}
                    )
                for lot in data.get("ledger_closed", []):
                    lots.append(
                        {"strategy_id": owner, "symbol": symbol, "leg": leg, **lot}
                    )
    return {
        "lots": lots,
        "metadata": report.get("metadata", {}),
        "orphan_overlay": report.get("orphan_overlay"),
    }


@router.get("/volume")
async def get_volume(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    window: str = Query("24h", description="Time window for calculation"),
) -> MetricResponse:
    """
    Get volume metrics for a trading pair.

    Includes total volume, moving averages, delta, and spikes.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_volume"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=10
        )

        values = [
            {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "total_volume": str(r.get("total_volume", "0")),
                "volume_sma": str(r.get("volume_sma", "0")),
                "volume_ema": str(r.get("volume_ema", "0")),
                "volume_delta": str(r.get("volume_delta", "0")),
                "volume_spike_ratio": str(r.get("volume_spike_ratio", "1.0")),
            }
            for r in results
        ]

        return MetricResponse(
            pair=pair,
            period=period,
            metric="volume",
            method="aggregation",
            window=window,
            values=values,
            metadata={
                "data_completeness": 100.0,
                "last_updated": datetime.now(UTC).isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching volume metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/spread")
async def get_spread(
    pair: str = Query(..., description="Trading pair symbol"),
) -> dict:
    """
    Get spread and liquidity metrics for a trading pair.

    Includes bid-ask spread, market depth, and liquidity ratio.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_spread"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            return {
                "pair": pair,
                "metric": "spread",
                "data": None,
                "metadata": {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "source": "mongodb",
                    "message": "No spread data available",
                },
            }

        r = results[0]

        return {
            "pair": pair,
            "metric": "spread",
            "data": {
                "bid_ask_spread": str(r.get("bid_ask_spread", "0")),
                "spread_percentage": str(r.get("spread_percentage", "0")),
                "market_depth_bid": str(r.get("market_depth_bid", "0")),
                "market_depth_ask": str(r.get("market_depth_ask", "0")),
                "liquidity_ratio": str(r.get("liquidity_ratio", "0")),
                "slippage_estimate": (
                    str(r.get("slippage_estimate"))
                    if r.get("slippage_estimate")
                    else None
                ),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "source": "mongodb",
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching spread metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trend")
async def get_trend(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    window: str = Query("20", description="Window size for moving averages"),
) -> MetricResponse:
    """
    Get trend and momentum indicators for a trading pair.

    Includes SMA, EMA, WMA, rate of change, and directional strength.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_trend"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=10
        )

        values = [
            {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "sma": str(r.get("sma", "0")),
                "ema": str(r.get("ema", "0")),
                "wma": str(r.get("wma", "0")),
                "rate_of_change": str(r.get("rate_of_change", "0")),
                "directional_strength": str(r.get("directional_strength", "50")),
                "crossover_signal": r.get("crossover_signal"),
            }
            for r in results
        ]

        return MetricResponse(
            pair=pair,
            period=period,
            metric="trend",
            method="moving_averages",
            window=window,
            values=values,
            metadata={
                "data_completeness": 100.0,
                "last_updated": datetime.now(UTC).isoformat(),
                "collection": collection,
                "records_returned": len(values),
            },
        )

    except Exception as e:
        logger.error(f"Error fetching trend metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/correlation")
async def get_correlation(
    pairs: str = Query(..., description="Comma-separated list of trading pairs"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
    window: str = Query("30d", description="Time window for correlation"),
) -> dict:
    """
    Get correlation matrix for multiple trading pairs.

    Returns pairwise correlation coefficients.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        pair_list = [p.strip() for p in pairs.split(",")]

        # Query correlation matrix
        collection = "analytics_correlation_matrix"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, limit=1
        )

        if results and results[0].get("matrix"):
            correlation_matrix = results[0].get("matrix")
        else:
            correlation_matrix = {}

        return {
            "pairs": pair_list,
            "period": period,
            "metric": "correlation",
            "method": "pearson",
            "window": window,
            "correlation_matrix": correlation_matrix,
            "metadata": {
                "data_completeness": 100.0,
                "last_updated": datetime.now(UTC).isoformat(),
                "collection": collection,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching correlation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/deviation")
async def get_deviation(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
) -> dict:
    """
    Get deviation and statistical metrics for a trading pair.

    Includes Bollinger Bands, Z-Score, autocorrelation.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_deviation"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            raise HTTPException(
                status_code=404, detail=f"No deviation data available for {pair}"
            )

        r = results[0]

        return {
            "pair": pair,
            "metric": "deviation",
            "data": {
                "standard_deviation": str(r.get("standard_deviation", "0")),
                "variance": str(r.get("variance", "0")),
                "z_score": str(r.get("z_score", "0")),
                "bollinger_upper": str(r.get("bollinger_upper", "0")),
                "bollinger_lower": str(r.get("bollinger_lower", "0")),
                "price_range_index": str(r.get("price_range_index", "0")),
                "autocorrelation": (
                    str(r.get("autocorrelation")) if r.get("autocorrelation") else None
                ),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching deviation metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/seasonality")
async def get_seasonality(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query(..., description="Data period (e.g., '1h', '1d')"),
) -> dict:
    """
    Get seasonality and cyclical patterns for a trading pair.

    Includes hourly/daily patterns, Fourier analysis, entropy.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_seasonality"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            raise HTTPException(
                status_code=404, detail=f"No seasonality data available for {pair}"
            )

        r = results[0]

        return {
            "pair": pair,
            "metric": "seasonality",
            "data": {
                "hourly_pattern": {
                    k: str(v) for k, v in r.get("hourly_pattern", {}).items()
                },
                "daily_pattern": {
                    k: str(v) for k, v in r.get("daily_pattern", {}).items()
                },
                "seasonal_deviation": str(r.get("seasonal_deviation", "0")),
                "entropy_index": str(r.get("entropy_index", "0")),
                "dominant_cycle": r.get("dominant_cycle"),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching seasonality metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/regime")
async def get_regime(
    pair: str = Query(..., description="Trading pair symbol"),
    period: str = Query("1h", description="Data period (e.g., '1h', '1d')"),
) -> dict:
    """
    Get market regime classification for a trading pair.

    Query params:
      - pair: trading pair symbol (e.g. "BTCUSDT")
      - period: data period hint (e.g. "1h", "1d") — stored in the analytics collection name

    Response shape (200 OK):
      {pair, metric="regime", data: {regime, volatility_level, volume_level,
       trend_direction, confidence} | null, metadata: {timestamp, collection}}

    When no regime has been computed yet for the pair, `data` is null and the
    status is still 200. Callers must treat null `data` as "regime unknown" —
    NOT as a wiring error. A 404 from this endpoint always means the route
    itself is missing, never that data is absent.

    503 → MongoDB adapter unavailable.
    500 → Unexpected server error.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        collection = f"analytics_{pair}_regime"
        results = await api_module.db_manager.mongodb_adapter.query_latest(
            collection, symbol=pair, limit=1
        )

        if not results:
            return {
                "pair": pair,
                "metric": "regime",
                "data": None,
                "metadata": {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "collection": collection,
                },
            }

        r = results[0]

        return {
            "pair": pair,
            "metric": "regime",
            "data": {
                "regime": r.get("regime", "unknown"),
                "volatility_level": r.get("volatility_level", "unknown"),
                "volume_level": r.get("volume_level", "unknown"),
                "trend_direction": r.get("trend_direction", "neutral"),
                "confidence": str(r.get("confidence", "0.5")),
            },
            "metadata": {
                "timestamp": (
                    r.get("metadata", {})
                    .get("computed_at", datetime.now(UTC))
                    .isoformat()
                    if isinstance(r.get("metadata", {}).get("computed_at"), datetime)
                    else str(r.get("metadata", {}).get("computed_at", ""))
                ),
                "collection": collection,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching regime: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/market-overview")
async def market_overview(
    pairs: str = Query(
        "BTCUSDT,ETHUSDT", description="Comma-separated list of trading pairs"
    ),
    limit: int = Query(
        10,
        ge=1,
        le=100,
        description="Maximum number of pairs to return (default: 10, max: 100)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_by: str = Query(
        "symbol", description="Sort by field (symbol, volatility, volume, trend)"
    ),
    sort_order: str = Query("asc", description="Sort order (asc, desc)"),
) -> dict:
    """
    Get comprehensive market overview for multiple pairs with pagination.

    Returns volatility, volume, trend, and regime for each pair.
    Supports pagination and sorting for efficient data retrieval.
    """
    if not api_module.db_manager or not api_module.db_manager.mongodb_adapter:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        pair_list = [p.strip() for p in pairs.split(",")]

        # Apply pagination to pair list
        total_pairs = len(pair_list)
        paginated_pair_list = pair_list[offset : offset + limit]

        overview = {}

        for pair in paginated_pair_list:
            try:
                # Get latest metrics for each type
                vol_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_volatility", symbol=pair, limit=1
                )
                volume_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_volume", symbol=pair, limit=1
                )
                trend_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_trend", symbol=pair, limit=1
                )
                regime_data = await api_module.db_manager.mongodb_adapter.query_latest(
                    f"analytics_{pair}_regime", symbol=pair, limit=1
                )

                overview[pair] = {
                    "volatility": {
                        "annualized": (
                            str(vol_data[0].get("annualized_volatility", "0"))
                            if vol_data
                            else "0"
                        ),
                    },
                    "volume": {
                        "spike_ratio": (
                            str(volume_data[0].get("volume_spike_ratio", "1.0"))
                            if volume_data
                            else "1.0"
                        ),
                    },
                    "trend": {
                        "direction": (
                            trend_data[0].get("crossover_signal", "neutral")
                            if trend_data
                            else "neutral"
                        ),
                        "roc": str(trend_data[0].get("rate_of_change", "0"))
                        if trend_data
                        else "0",
                    },
                    "regime": {
                        "classification": (
                            regime_data[0].get("regime", "unknown")
                            if regime_data
                            else "unknown"
                        ),
                        "confidence": (
                            str(regime_data[0].get("confidence", "0"))
                            if regime_data
                            else "0"
                        ),
                    },
                }

            except Exception as e:
                logger.warning(f"Error getting overview for {pair}: {e}")
                overview[pair] = None

        # Apply sorting if requested
        if sort_by != "symbol":
            try:
                # Convert overview dict to list of tuples for sorting
                overview_list = list(overview.items())
                if sort_by == "volatility":
                    overview_list.sort(
                        key=lambda x: (
                            float(x[1]["volatility"]["annualized"]) if x[1] else 0
                        ),
                        reverse=(sort_order == "desc"),
                    )
                elif sort_by == "volume":
                    overview_list.sort(
                        key=lambda x: (
                            float(x[1]["volume"]["spike_ratio"]) if x[1] else 0
                        ),
                        reverse=(sort_order == "desc"),
                    )
                elif sort_by == "trend":
                    overview_list.sort(
                        key=lambda x: float(x[1]["trend"]["roc"]) if x[1] else 0,
                        reverse=(sort_order == "desc"),
                    )
                overview = dict(overview_list)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Could not sort by {sort_by}: {e}")

        return {
            "overview": overview,
            "pagination": {
                "total": total_pairs,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1,
                "pages": (total_pairs + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_pairs,
                "has_previous": offset > 0,
            },
            "sort": {
                "by": sort_by,
                "order": sort_order,
            },
            "timestamp": datetime.now(UTC).isoformat(),
            "pairs_requested": len(paginated_pair_list),
            "pairs_available": len([v for v in overview.values() if v is not None]),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating market overview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
