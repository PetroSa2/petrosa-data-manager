"""Operator dashboard backend API surface (P5.1a, petrosa_k8s#644).

Stable HTTP surface the operator dashboard SPA consumes. Each route is a
thin adapter over the existing service-layer code that already powers
the `/api/v1/...` routes (`pnl.py`, `drawdown.py`, `audit.py`,
`lifecycle.py`, `portfolio_state.py`, `strategy_timeline.py`). The
dashboard prefix is `/api/dashboard/` so the SPA contract is decoupled
from the service-internal `/api/v1/` API and can evolve independently.

Routes (all `GET`, all JSON, errors via RFC 7807 problem JSON):

  * ``GET /api/dashboard/portfolio/pnl?window=24h[&strategy_id=...]`` —
    realized + unrealized at strategy or portfolio scope.
  * ``GET /api/dashboard/portfolio/drawdown?window=24h&strategy_id=...`` —
    current drawdown vs the characterized envelope.
  * ``GET /api/dashboard/audit/timeline?window=24h[&strategy=...&decision_id=...&action=...&symbol=...&limit=...]`` —
    cross-service decision audit trail with composable filters.
  * ``GET /api/dashboard/lifecycle/{decision_id}`` — full
    intents→decision→executions→pnl_events join for one decision.
  * ``GET /api/dashboard/portfolio/state?at=<iso_ts>[&strategy_id=...]`` —
    portfolio state + recent event chain at the given point-in-time.
  * ``GET /api/dashboard/strategy/{strategy_id}/lifecycle?window=24h[&limit=200]`` —
    per-strategy lifecycle timeline.

The ``window`` query parameter is a SPA-friendly shortcut for the
``from=now-window&to=now`` pair used by the underlying routes. Accepted
values: ``1h | 6h | 24h | 7d | 30d``. Omit ``window`` (or pass an
explicit ``from``/``to``) for full available history.

Out of scope for this PR (filed as a follow-up): the 2 ``petrosa-cio``
routes the ticket body lists (``GET /api/cio/decisions/recent`` and
``GET /api/cio/evaluator/verdicts``). Those live in a separate repo
(petrosa-cio) and warrant their own PR/lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

try:
    from datetime import UTC
except ImportError:  # Python 3.10 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from fastapi import APIRouter, HTTPException, Path, Query

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------------
# Helpers — shared across the dashboard routes.
# ----------------------------------------------------------------------


SUPPORTED_WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _resolve_window(window: str | None) -> tuple[datetime | None, datetime | None]:
    """Convert ``window=<token>`` into a (from, to) pair.

    Returns ``(None, None)`` when ``window`` is None — semantic for
    "no window filter, use full history". Otherwise both bounds are
    populated with ``to=now`` and ``from=now-delta``.
    """
    if window is None:
        return None, None
    delta = SUPPORTED_WINDOWS.get(window)
    if delta is None:
        _raise_problem(
            status=400,
            title="invalid_window",
            detail=(
                f"window={window!r} is not supported. Accepted values: "
                f"{sorted(SUPPORTED_WINDOWS.keys())}"
            ),
        )
    now = datetime.now(UTC)
    return now - delta, now


def _raise_problem(*, status: int, title: str, detail: str) -> None:
    """Raise an HTTPException whose JSON body is RFC 7807 shaped.

    FastAPI's default HTTPException body is ``{"detail": "..."}``. We
    wrap it in a richer dict so SPA consumers can grep on a stable
    machine-readable ``title`` while preserving the human-readable
    ``detail`` (matches the ticket's "errors return RFC 7807 problem
    JSON" acceptance criterion).
    """
    raise HTTPException(
        status_code=status,
        detail={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


def _require_mongo():
    if not api_module.db_manager or not getattr(
        api_module.db_manager, "mongodb_adapter", None
    ):
        _raise_problem(
            status=503,
            title="database_unavailable",
            detail="MongoDB is not configured or not yet connected.",
        )
    return api_module.db_manager.mongodb_adapter


# ----------------------------------------------------------------------
# Route 1: /portfolio/pnl — realized + unrealized P&L.
# ----------------------------------------------------------------------


@router.get("/portfolio/pnl")
async def dashboard_portfolio_pnl(
    window: str | None = Query(
        "24h",
        description=(
            "Time window for the P&L replay. One of "
            "'1h', '6h', '24h', '7d', '30d'. Omit (or pass null) for "
            "full history."
        ),
    ),
    strategy_id: str | None = Query(
        None,
        description=(
            "Strategy filter. Omit for portfolio-wide aggregation across "
            "every strategy."
        ),
    ),
) -> dict:
    """Realized + unrealized P&L over the given window."""
    from data_manager.services.pnl_calculator import PnlCalculator

    mongodb = _require_mongo()
    from_ts, to_ts = _resolve_window(window)

    query: dict = {"event_type": {"$in": ["filled", "partial_fill"]}}
    if strategy_id:
        query["strategy_id"] = strategy_id
    if from_ts is not None or to_ts is not None:
        ts_range: dict = {}
        if from_ts is not None:
            ts_range["$gte"] = from_ts
        if to_ts is not None:
            ts_range["$lt"] = to_ts
        query["timestamp"] = ts_range

    try:
        cursor = mongodb.db["execution_events"].find(query).sort("timestamp", 1)
        rows = await cursor.to_list(length=None)
    except Exception as exc:
        logger.error("dashboard_pnl_read_failed: %s", exc, exc_info=True)
        _raise_problem(
            status=500,
            title="pnl_read_failed",
            detail="Failed to read execution_events for P&L computation.",
        )

    calc = PnlCalculator()
    for row in rows:
        calc.apply_fill(row)

    if strategy_id:
        breakdown = calc.strategy_pnl(strategy_id)
        scope = "strategy"
    else:
        breakdown = calc.portfolio_pnl()
        scope = "portfolio"

    return {
        "scope": scope,
        "strategy_id": strategy_id,
        "window": window,
        "from": from_ts.isoformat() if from_ts else None,
        "to": to_ts.isoformat() if to_ts else None,
        "realized_pnl_usd": breakdown.realized,
        "unrealized_pnl_usd": breakdown.unrealized,
        "total_pnl_usd": breakdown.total,
        "fill_count": len(rows),
    }


# ----------------------------------------------------------------------
# Route 2: /portfolio/drawdown — current drawdown vs envelope.
# ----------------------------------------------------------------------


@router.get("/portfolio/drawdown")
async def dashboard_portfolio_drawdown(
    strategy_id: str = Query(
        ..., description="Strategy identifier to evaluate (required)."
    ),
    window: str | None = Query(
        "24h",
        description=(
            "Time window for the drawdown computation. One of "
            "'1h', '6h', '24h', '7d', '30d'. Omit (or pass null) for full "
            "history."
        ),
    ),
) -> dict:
    """Current drawdown for ``strategy_id`` vs its characterized envelope."""
    from data_manager.db.repositories.characterization_repository import (
        CharacterizationRepository,
    )
    from data_manager.portfolio.drawdown_service import DrawdownService

    mongodb = _require_mongo()
    from_ts, to_ts = _resolve_window(window)

    try:
        char_repo = CharacterizationRepository(
            api_module.db_manager.mysql_adapter,
            mongodb,
        )
        service = DrawdownService(
            mongodb_adapter=mongodb,
            characterization_repository=char_repo,
        )
        result = await service.compute(
            strategy_id=strategy_id,
            start=from_ts,
            end=to_ts,
        )
    except Exception as exc:
        logger.error(
            "dashboard_drawdown_failed",
            extra={"strategy_id": strategy_id, "error": str(exc)},
            exc_info=True,
        )
        _raise_problem(
            status=500,
            title="drawdown_computation_failed",
            detail="Failed to compute drawdown for the given strategy.",
        )

    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    payload.setdefault("strategy_id", strategy_id)
    payload["window"] = window
    return payload


# ----------------------------------------------------------------------
# Route 3: /audit/timeline — filtered audit-trail rows.
# ----------------------------------------------------------------------


@router.get("/audit/timeline")
async def dashboard_audit_timeline(
    window: str | None = Query(
        "24h",
        description=(
            "Time window. One of '1h', '6h', '24h', '7d', '30d'. Omit for full history."
        ),
    ),
    from_time: datetime | None = Query(
        None,
        alias="from",
        description=("Explicit lower bound (ISO 8601). Overrides ``window`` when set."),
    ),
    to_time: datetime | None = Query(
        None,
        alias="to",
        description="Explicit upper bound (ISO 8601). Overrides ``window`` when set.",
    ),
    strategy: str | None = Query(
        None,
        alias="strategy",
        description="Filter by strategy_id (alias of strategy_id from /api/v1).",
    ),
    decision_id: str | None = Query(
        None,
        description="Filter by a single CIO decision_id.",
    ),
    action: str | None = Query(
        None,
        description=(
            "Filter by CIO action verb (execute / admit / veto / "
            "down_weight / fail_safe / skip)."
        ),
    ),
    symbol: str | None = Query(None, description="Filter by trading-pair symbol."),
    limit: int = Query(
        100, ge=1, le=1000, description="Max rows returned (1..1000, default 100)."
    ),
) -> dict:
    """List CIO decision audit-trail rows, newest-first, with composable filters."""
    from data_manager.db.repositories import AuditRepository

    mongodb = _require_mongo()
    # Explicit from/to wins over the window shortcut.
    if from_time is None and to_time is None:
        from_time, to_time = _resolve_window(window)

    try:
        repo = AuditRepository(
            api_module.db_manager.mysql_adapter,
            mongodb,
        )
        rows = await repo.query_decisions(
            strategy_id=strategy,
            action=action,
            decision_id=decision_id,
            symbol=symbol,
            start=from_time,
            end=to_time,
            limit=limit,
        )
    except Exception as exc:
        logger.error(
            "dashboard_audit_timeline_failed",
            extra={"error": str(exc)},
            exc_info=True,
        )
        _raise_problem(
            status=500,
            title="audit_query_failed",
            detail="Failed to query the audit trail.",
        )

    return {
        "window": window,
        "from": from_time.isoformat() if from_time else None,
        "to": to_time.isoformat() if to_time else None,
        "filters": {
            "strategy": strategy,
            "decision_id": decision_id,
            "action": action,
            "symbol": symbol,
        },
        "limit": limit,
        "count": len(rows),
        "decisions": rows,
    }


# ----------------------------------------------------------------------
# Route 4: /lifecycle/{decision_id} — full per-decision lifecycle.
# ----------------------------------------------------------------------


@router.get("/lifecycle/{decision_id}")
async def dashboard_lifecycle_by_decision(
    decision_id: str = Path(..., description="CIO decision identifier."),
) -> dict:
    """Full lifecycle (intents + decision + execution_events + pnl_events)."""
    from data_manager.db.repositories.lifecycle_repository import (
        LifecycleRepository,
    )

    mongodb = _require_mongo()
    try:
        repo = LifecycleRepository(
            api_module.db_manager.mysql_adapter,
            mongodb,
        )
        result = await repo.reconstruct(decision_id)
    except Exception as exc:
        logger.error(
            "dashboard_lifecycle_failed",
            extra={"decision_id": decision_id, "error": str(exc)},
            exc_info=True,
        )
        _raise_problem(
            status=500,
            title="lifecycle_reconstruction_failed",
            detail="Failed to reconstruct lifecycle for the given decision_id.",
        )

    if result is None:
        _raise_problem(
            status=404,
            title="decision_not_found",
            detail=f"No decision found for decision_id={decision_id!r}.",
        )
    return result


# ----------------------------------------------------------------------
# Route 5: /portfolio/state — state at point-in-time T.
# ----------------------------------------------------------------------


@router.get("/portfolio/state")
async def dashboard_portfolio_state(
    at: datetime = Query(
        ...,
        description="Point-in-time timestamp (ISO 8601, exclusive upper bound).",
    ),
    strategy_id: str | None = Query(
        None,
        description=(
            "Optional strategy filter. Omit for portfolio-wide state "
            "across every strategy."
        ),
    ),
) -> dict:
    """Portfolio state + recent event chain at the requested timestamp."""
    from data_manager.portfolio.state_service import PortfolioStateService

    mongodb = _require_mongo()
    try:
        service = PortfolioStateService(mongodb_adapter=mongodb)
        result = await service.state_at(at, strategy_id=strategy_id)
    except Exception as exc:
        logger.error(
            "dashboard_portfolio_state_failed",
            extra={"at": at.isoformat(), "error": str(exc)},
            exc_info=True,
        )
        _raise_problem(
            status=500,
            title="portfolio_state_failed",
            detail="Failed to compute portfolio state at the requested timestamp.",
        )

    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


# ----------------------------------------------------------------------
# Route 6: /strategy/{strategy_id}/lifecycle — per-strategy timeline.
# ----------------------------------------------------------------------


@router.get("/strategy/{strategy_id}/lifecycle")
async def dashboard_strategy_lifecycle(
    strategy_id: str = Path(..., description="Strategy identifier."),
    window: str | None = Query(
        "24h",
        description=(
            "Time window. One of '1h', '6h', '24h', '7d', '30d'. Omit for full history."
        ),
    ),
    types: str | None = Query(
        None,
        description=(
            "Comma-separated event types to include. Defaults to all "
            "types known by the timeline repository."
        ),
    ),
    limit: int = Query(
        200, ge=1, le=1000, description="Page size (1..1000, default 200)."
    ),
    cursor: str | None = Query(
        None,
        description="Opaque cursor for pagination (from a prior page's next_cursor).",
    ),
) -> dict:
    """Per-strategy lifecycle timeline (intents, decisions, executions, P&L)."""
    from data_manager.db.repositories.strategy_timeline_repository import (
        ALL_TYPES,
        StrategyTimelineRepository,
    )

    mongodb = _require_mongo()
    from_ts, to_ts = _resolve_window(window)

    type_filter: list[str] | None = None
    if types is not None:
        candidates = [t.strip() for t in types.split(",") if t.strip()]
        invalid = set(candidates) - set(ALL_TYPES)
        if invalid:
            _raise_problem(
                status=400,
                title="invalid_event_type",
                detail=(
                    f"Unknown event types: {sorted(invalid)}. "
                    f"Allowed: {sorted(ALL_TYPES)}"
                ),
            )
        type_filter = candidates

    try:
        repo = StrategyTimelineRepository(
            mysql_adapter=api_module.db_manager.mysql_adapter,
            mongodb_adapter=mongodb,
        )
        result = await repo.get_timeline(
            strategy_id=strategy_id,
            from_ts=from_ts,
            to_ts=to_ts,
            types=type_filter,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        logger.error(
            "dashboard_strategy_lifecycle_failed",
            extra={"strategy_id": strategy_id, "error": str(exc)},
            exc_info=True,
        )
        _raise_problem(
            status=500,
            title="strategy_lifecycle_failed",
            detail="Failed to query the strategy timeline.",
        )

    payload = result if isinstance(result, dict) else {"events": result}
    payload.setdefault("strategy_id", strategy_id)
    payload["window"] = window
    return payload
