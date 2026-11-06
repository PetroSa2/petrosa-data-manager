"""Which per-strategy stats CIO is served: ``netting`` (today) or ``ground_truth`` (petrosa-data-manager#842).

The source is ``parameters.strategy_stats_source`` of the application config, set only through
:func:`set_source`, which writes an audited config version (who, when, the user decision link). Reads are cached
for ``SOURCE_CACHE_SECONDS``, so a flip takes effect within a minute and a flip back is just as fast: no deploy.
An unset or unknown value is ``netting``, the behaviour that ran before this switch existed.

With ``ground_truth`` the latest exchange-truth snapshot is served in the two contracts CIO already reads. A
missing or stale snapshot (older than two refresh cycles) is answered as degraded with no stats, never with old
numbers; a strategy with no closed episode in the window gets no win rate (unknown), never zeros.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from data_manager.services.strategy_stats_snapshot_store import (
    REFRESH_INTERVAL_SECONDS,
    is_stale,
)

NETTING = "netting"
GROUND_TRUTH = "ground_truth"
SOURCES = (NETTING, GROUND_TRUTH)
CONFIG_KEY = "strategy_stats_source"
SOURCE_CACHE_SECONDS = 60.0
#: The switch is read on the request path (once a minute): never wait on MongoDB longer than this.
SOURCE_READ_TIMEOUT_SECONDS = 2.0
#: The contract value CIO recognises for computed performance stats (cio ``DM_PERFORMANCE_COMPUTED_SOURCE``).
COMPUTED_SOURCE = "data-manager-pnl-calculator"

logger = logging.getLogger(__name__)

_cache: tuple[float, str] | None = None
#: The last value read successfully in this process. A failed read serves it (never a silent fall back to
#: netting after a flip); only a process that has never read the config falls back to ``netting``.
_last_read: str | None = None


def _normalise(value: Any) -> str:
    return value if value in SOURCES else NETTING


async def get_source(db_manager: Any, clock=time.monotonic) -> str:
    """The configured source; ``netting`` when unset or unknown.

    A failed or timed-out read serves the last value read successfully in this process, even past its cache
    window, so a transient MongoDB failure after a flip to ``ground_truth`` never serves netting stats.
    Only a process that has never read the config falls back to ``netting``.
    """
    global _cache, _last_read
    now = clock()
    if _cache is not None and now - _cache[0] < SOURCE_CACHE_SECONDS:
        return _cache[1]
    configuration = getattr(db_manager, "configuration", None)
    if configuration is None:
        source = _last_read or NETTING
    else:
        try:
            async with asyncio.timeout(SOURCE_READ_TIMEOUT_SECONDS):
                config = await configuration.get_app_config()
            parameters = config.get("parameters") if isinstance(config, dict) else None
            source = _normalise(
                parameters.get(CONFIG_KEY) if isinstance(parameters, dict) else None
            )
            _last_read = source
        except Exception as exc:  # an unreadable config never breaks the route
            source = _last_read or NETTING
            logger.warning(
                "strategy_stats_source: config read failed (%s: %s); serving %s (%s)",
                type(exc).__name__,
                exc,
                source,
                "last read value" if _last_read else "never read: default",
            )
    _cache = (now, source)
    return source


def reset_cache() -> None:
    """Drop the cached value (the next call reads the config); the last read value is kept."""
    global _cache
    _cache = None


def reset_state() -> None:
    """Forget everything read in this process (tests)."""
    global _last_read
    reset_cache()
    _last_read = None


async def set_source(
    db_manager: Any, source: str, *, changed_by: str, decision_ref: str
) -> dict[str, Any]:
    """Write ``source`` into the application config as a new audited version; returns the new config."""
    global _last_read
    if source not in SOURCES:
        raise ValueError(f"strategy_stats_source must be one of {SOURCES}")
    if not changed_by.strip() or not decision_ref.strip():
        raise ValueError("changed_by and decision_ref are required")
    configuration = getattr(db_manager, "configuration", None)
    if configuration is None:
        raise RuntimeError("configuration repository unavailable")
    existing = await configuration.get_app_config() or {}
    parameters = {**(existing.get("parameters") or {}), CONFIG_KEY: source}
    config = await configuration.upsert_app_config(
        parameters=parameters,
        changed_by=changed_by,
        reason=f"{CONFIG_KEY} -> {source}; decision: {decision_ref}",
    )
    if not config:
        raise RuntimeError("application config update failed")
    reset_cache()
    _last_read = source
    return config


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _degraded(
    strategy_id: str, kind: str, message: str, snapshot: dict | None
) -> dict[str, Any]:
    error = {"type": kind, "message": message}
    return {
        "degraded": True,
        "error": error,
        "stats": {
            "win_rate": None,
            "win_rate_delta": None,
            "win_rate_delta_window": None,
            "win_rate_delta_se": None,
            "wins": None,
            "losses": None,
            "consecutive_losses": None,
            "recent_pnl_trend": "neutral",
            "realized_pnl": None,
            "unrealized_pnl": None,
        },
        "metadata": {
            "strategy_id": strategy_id,
            "calculated_at": datetime.now(UTC).isoformat(),
            "source": "data-manager-exchange-ground-truth",
            "stats_source": GROUND_TRUTH,
            "fills_replayed": 0,
            "history_status": "unavailable",
            "degraded": True,
            "error": error,
            "snapshot_computed_at": _iso((snapshot or {}).get("computed_at")),
            "snapshot_refresh_seconds": REFRESH_INTERVAL_SECONDS,
        },
    }


def performance_from_snapshot(
    snapshot: dict[str, Any] | None, strategy_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """``/analysis/performance/{strategy}`` from the exchange-truth snapshot."""
    if is_stale(snapshot, now):
        return _degraded(
            strategy_id,
            "GroundTruthStatsStale",
            "no exchange-truth stats snapshot within two refresh cycles",
            snapshot,
        )
    entry = (snapshot.get("strategies") or {}).get(strategy_id)
    performance = (entry or {}).get("performance")
    rounds = (entry or {}).get("rounds") or {}
    stats = (
        dict(performance)
        if performance
        else _degraded(strategy_id, "", "", None)["stats"]
    )
    computed = (
        stats.get("win_rate") is not None
        and stats.get("consecutive_losses") is not None
    )
    return {
        "stats": stats,
        "metadata": {
            "strategy_id": strategy_id,
            "calculated_at": _iso(snapshot.get("computed_at")),
            "source": COMPUTED_SOURCE,
            "stats_source": GROUND_TRUTH,
            "rule": snapshot.get("rule"),
            "window": snapshot.get("window"),
            "fills_replayed": int(rounds.get("fills") or 0),
            "history_status": (
                "computed"
                if computed
                else "zero_sample"
                if not entry
                else "insufficient_history"
            ),
            "snapshot_computed_at": _iso(snapshot.get("computed_at")),
            "snapshot_refresh_seconds": REFRESH_INTERVAL_SECONDS,
        },
    }


def rounds_from_snapshot(
    snapshot: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """``/analysis/rounds`` (all strategies, 30 days) from the exchange-truth snapshot."""
    if is_stale(snapshot, now):
        return {
            "strategies": {},
            "degraded": True,
            "error": {
                "type": "GroundTruthStatsStale",
                "message": "no exchange-truth stats snapshot within two refresh cycles",
            },
            "metadata": {
                "stats_source": GROUND_TRUTH,
                "snapshot_computed_at": _iso((snapshot or {}).get("computed_at")),
                "stale": True,
            },
        }
    strategies = {
        name: entry.get("rounds") or {}
        for name, entry in (snapshot.get("strategies") or {}).items()
    }
    return {
        "strategies": strategies,
        "totals": {
            "closed_rounds": sum(
                int(r.get("closed_rounds") or 0) for r in strategies.values()
            ),
        },
        "reconciliation": snapshot.get("reconciliation"),
        "metadata": {
            "calculated_at": _iso(snapshot.get("computed_at")),
            "computed_at": _iso(snapshot.get("computed_at")),
            "source": "data-manager-exchange-ground-truth",
            "stats_source": GROUND_TRUTH,
            "rule": snapshot.get("rule"),
            "window": snapshot.get("window"),
            "stale": False,
        },
    }


def net_r_from_snapshot(
    snapshot: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """``/analysis/strategy-net-r`` (CIO's keep/kill input) from the exchange-truth snapshot.

    Stale or missing: no strategies, degraded; CIO's keep/kill then evaluates nothing (no verdict changes),
    never a "keep" from old data.
    """
    if is_stale(snapshot, now):
        return {
            "strategies": {},
            "degraded": True,
            "error": {
                "type": "GroundTruthStatsStale",
                "message": "no exchange-truth stats snapshot within two refresh cycles",
            },
            "metadata": {"stats_source": GROUND_TRUTH, "stale": True},
        }
    strategies = {
        name: entry["net_r"]
        for name, entry in (snapshot.get("strategies") or {}).items()
        if isinstance(entry.get("net_r"), dict)
    }
    return {
        "strategies": strategies,
        "metadata": {
            "calculated_at": _iso(snapshot.get("computed_at")),
            "source": "data-manager-exchange-ground-truth",
            "stats_source": GROUND_TRUTH,
            "rule": snapshot.get("rule"),
            "stale": False,
        },
    }
