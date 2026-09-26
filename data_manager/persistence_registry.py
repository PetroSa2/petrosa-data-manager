"""Machine-readable persistence policy for MongoDB collections."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class PersistenceSpec:
    """Persistence classification for one exact collection or prefix."""

    classification: Literal["durable", "transient_only"]
    mysql_table: str | None = None
    key: str | None = None
    reason: str | None = None
    pending: bool = False


def durable(*, mysql_table: str, key: str, pending: bool = False) -> PersistenceSpec:
    """Declare a Mongo collection whose long-lived copy is in MySQL."""

    return PersistenceSpec(
        classification="durable",
        mysql_table=mysql_table,
        key=key,
        pending=pending,
    )


def transient_only(*, reason: str) -> PersistenceSpec:
    """Declare a collection that is a cache, coordination store, or recomputable view."""

    return PersistenceSpec(classification="transient_only", reason=reason)


REGISTRY: dict[str, PersistenceSpec] = {
    "signals": durable(mysql_table="signals", key="symbol+timestamp"),
    "cio_decisions": durable(mysql_table="cio_decisions", key="decision_id"),
    "execution_events": durable(
        mysql_table="execution_events",
        key="order_id+event_type+timestamp",
        pending=True,
    ),
    "pnl_events": durable(
        mysql_table="pnl_events", key="decision_id+pnl_kind+timestamp", pending=True
    ),
    "intents": durable(mysql_table="intents", key="intent_id", pending=True),
    "alerts": durable(
        mysql_table="alerts", key="category+dedupe_key+timestamp", pending=True
    ),
    "trades": durable(mysql_table="trades", key="symbol+timestamp", pending=True),
    "positions": durable(mysql_table="positions", key="position_id"),
    "daily_pnl": durable(mysql_table="daily_pnl", key="date"),
    "leader_election": transient_only(reason="coordination lease; safe to recreate"),
    "distributed_locks": transient_only(reason="coordination lock; safe to recreate"),
    "service_leases": transient_only(reason="coordination lease API; safe to recreate"),
    "config_rate_limits": transient_only(
        reason="bounded sliding-window rate-limit cache"
    ),
    "tradeengine_boot_probes": transient_only(reason="diagnostic probe state"),
    "_atlas_quota_probe": transient_only(reason="diagnostic quota probe state"),
    "_p0_unblock_probe": transient_only(reason="diagnostic unblock probe state"),
    "strategy_registry": transient_only(
        reason="operator registry reloadable from configuration"
    ),
    "schemas": transient_only(reason="schema metadata can be recreated from source"),
    "app_config": transient_only(reason="runtime configuration; managed by deployment"),
    "app_config_audit": transient_only(reason="configuration audit view"),
    "strategy_configs_global": transient_only(
        reason="runtime configuration; managed by deployment"
    ),
    "strategy_configs_symbol": transient_only(
        reason="runtime configuration; managed by deployment"
    ),
    "strategy_config_audit": transient_only(reason="configuration audit view"),
    "strategy_lifecycle_events": transient_only(
        reason="rebuildable lifecycle projection"
    ),
    "trading_configs_global": transient_only(
        reason="runtime configuration; managed by deployment"
    ),
    "trading_configs_symbol": transient_only(
        reason="runtime configuration; managed by deployment"
    ),
    "trading_configs_symbol_side": transient_only(
        reason="runtime configuration; managed by deployment"
    ),
    "trading_configs_audit": transient_only(reason="configuration audit view"),
    "characterizations": transient_only(
        reason="recomputable strategy characterization"
    ),
    "characterization_artifacts": transient_only(
        reason="recomputable characterization artifact"
    ),
    "drawdown_breaches": transient_only(reason="recomputable risk projection"),
    "envelopes": transient_only(reason="versioned runtime configuration"),
    "pending_envelope_changes": transient_only(reason="operator workflow state"),
    "leverage_bounds": transient_only(reason="versioned runtime configuration"),
    "leverage_status": transient_only(reason="recomputable risk status"),
    "envelope_authorship_audit": transient_only(reason="configuration audit view"),
    "leverage_bounds_audit": transient_only(reason="configuration audit view"),
    "restore_exercises": transient_only(reason="operator recovery exercise state"),
}

PREFIX_REGISTRY: tuple[tuple[str, PersistenceSpec], ...] = (
    (
        "funding_rates_",
        durable(mysql_table="funding_rates", key="symbol+timestamp", pending=True),
    ),
    ("candles_", transient_only(reason="recomputable from durable MySQL klines")),
    ("klines_", transient_only(reason="durable market data is held in MySQL klines")),
    ("analytics_", transient_only(reason="recomputable from klines")),
    ("trades_", transient_only(reason="raw market data is owned by the extractor")),
    (
        "depth_",
        transient_only(reason="market-depth cache; not retained by this service"),
    ),
    (
        "tickers_",
        transient_only(reason="market-ticker cache; not retained by this service"),
    ),
    (
        "admin.",
        transient_only(reason="MongoDB system namespace; never application data"),
    ),
    (
        "config.",
        transient_only(reason="MongoDB system namespace; never application data"),
    ),
    (
        "local.",
        transient_only(reason="MongoDB system namespace; never application data"),
    ),
    (
        "system.",
        transient_only(reason="MongoDB system namespace; never application data"),
    ),
)


def entry_for_collection(collection: str) -> PersistenceSpec | None:
    """Return the exact or prefix policy for ``collection``."""

    if collection in REGISTRY:
        return REGISTRY[collection]
    for prefix, spec in PREFIX_REGISTRY:
        if collection.startswith(prefix):
            return spec
    return None


def durable_mysql_tables() -> frozenset[str]:
    """Return MySQL tables protected from destructive maintenance jobs."""

    specs = (*REGISTRY.values(), *(spec for _, spec in PREFIX_REGISTRY))
    return frozenset(spec.mysql_table for spec in specs if spec.mysql_table is not None)


def _literal_strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):  # noqa: UP038
        values: set[str] = set()
        for element in node.elts:
            values.update(_literal_strings(element))
        return values
    return set()


def scan_mongo_collection_names(root: str | Path) -> set[str]:
    """Find static Mongo collection names used by ``data_manager``."""

    found: set[str] = set()
    for path in sorted(Path(root).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue

        constants: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):  # noqa: UP038
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                value = node.value
                if value is None:
                    continue
                for target in targets:
                    if (
                        isinstance(target, ast.Name)
                        and "COLLECTION" in target.id.upper()
                    ):
                        values = _literal_strings(value)
                        if values:
                            constants[target.id] = values
                            found.update(values)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            base = node.value
            is_db_access = isinstance(base, ast.Name) and base.id == "db"
            is_db_access = is_db_access or (
                isinstance(base, ast.Attribute) and base.attr == "db"
            )
            if not is_db_access:
                continue
            if isinstance(node.slice, ast.Name):
                found.update(constants.get(node.slice.id, set()))
            else:
                found.update(_literal_strings(node.slice))
    return found


def unregistered_collections(root: str | Path) -> set[str]:
    """Return static collection names without an exact or prefix policy."""

    return {
        name
        for name in scan_mongo_collection_names(root)
        if entry_for_collection(name) is None
    }


__all__ = [
    "PREFIX_REGISTRY",
    "REGISTRY",
    "PersistenceSpec",
    "durable",
    "durable_mysql_tables",
    "entry_for_collection",
    "scan_mongo_collection_names",
    "transient_only",
    "unregistered_collections",
]
