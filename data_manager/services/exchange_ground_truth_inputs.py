"""Inputs for the exchange ground truth (dm#844), read on demand: no new collection.

- **Strategy position rows**: Mongo ``strategy_positions`` (the live store; the MySQL copy
  is frozen), the rows whose entry or exit falls in the window.
- **Order journal**: MySQL ``execution_events`` (the historic copy of every execution
  event, indexed by strategy and time). It gives order -> strategy for ``placed`` /
  ``filled`` / ``position_closed`` events, and entry order -> exit orders from
  ``position_closed`` payloads.
- **Exchange history**: fills and orders per symbol from tradeengine's read-only routes
  (:mod:`data_manager.services.exchange_history_client`).

The report itself is built from the exchange's own flat-to-flat episodes
(:mod:`data_manager.services.exchange_episodes`); ledger rows only annotate them.

Nothing is written anywhere.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text

from data_manager.services.exchange_episodes import build_ground_truth_v2
from data_manager.services.exchange_history_client import ExchangeHistoryClient

# Read caps: exceeding one is an explicit error, never a silently cut window.
MAX_POSITION_ROWS = 50_000
MAX_JOURNAL_ROWS = 200_000
# The journal is read with a margin around the window: an order placed just before the
# window can be closed inside it.
JOURNAL_MARGIN = timedelta(days=1)
_JOURNAL_EVENTS = ("placed", "filled", "position_closed")
_POSITION_FIELDS = {
    "_id": 0,
    "strategy_position_id": 1,
    "strategy_id": 1,
    "symbol": 1,
    "side": 1,
    "status": 1,
    "entry_order_id": 1,
    "exit_order_id": 1,
    "tp_order_id": 1,
    "sl_order_id": 1,
    "entry_time": 1,
    "exit_time": 1,
    "entry_quantity": 1,
    # Risk at entry for the exchange-truth net R (petrosa-data-manager#842): the entry price and the stop the
    # row carried. Entries are reliable on these rows; te#838 only affects exits.
    "entry_price": 1,
    "stop_loss_price": 1,
    "close_reason": 1,
}


class GroundTruthInputError(RuntimeError):
    """An input could not be read completely."""


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _in_window(row: dict[str, Any], start: datetime, end: datetime) -> bool:
    entry = _as_utc(row.get("entry_time"))
    exit_ = _as_utc(row.get("exit_time"))
    return any(t is not None and start <= t <= end for t in (entry, exit_))


async def read_position_rows(
    mongodb: Any, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Strategy position rows with an entry or exit in ``[start, end]``.

    Filtered here, not in the query: the stored times mix datetimes and ISO strings,
    and a typed range filter would silently miss one kind.
    """
    cursor = mongodb.db["strategy_positions"].find({}, _POSITION_FIELDS)
    rows = await cursor.limit(MAX_POSITION_ROWS + 1).to_list(
        length=MAX_POSITION_ROWS + 1
    )
    if len(rows) > MAX_POSITION_ROWS:
        raise GroundTruthInputError(
            f"strategy_positions read exceeded {MAX_POSITION_ROWS} rows"
        )
    return [row for row in rows if _in_window(row, start, end)]


def read_order_journal(
    mysql: Any, start: datetime, end: datetime
) -> tuple[dict[str, str], dict[str, set[str]], set[str], list[str]]:
    """Order -> strategy, entry order -> exit orders, symbols, and conflicting orders."""
    engine = mysql._ensure_connected()
    statement = text(
        "SELECT order_id, strategy_id, event_type, symbol, payload "
        "FROM execution_events "
        "WHERE timestamp >= :start AND timestamp <= :end "
        "AND event_type IN :events "
        "LIMIT :cap"
    ).bindparams(bindparam("events", expanding=True))
    params = {
        "events": list(_JOURNAL_EVENTS),
        "start": (start - JOURNAL_MARGIN).replace(tzinfo=None),
        "end": (end + JOURNAL_MARGIN).replace(tzinfo=None),
        "cap": MAX_JOURNAL_ROWS + 1,
    }
    with engine.begin() as connection:
        rows = connection.execute(statement, params).mappings().all()
    if len(rows) > MAX_JOURNAL_ROWS:
        raise GroundTruthInputError(
            f"execution_events read exceeded {MAX_JOURNAL_ROWS} rows"
        )

    order_strategy: dict[str, str] = {}
    conflicts: set[str] = set()
    exits: dict[str, set[str]] = {}
    symbols: set[str] = set()
    for row in rows:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if row.get("symbol"):
            symbols.add(str(row["symbol"]))
        strategy = str(row.get("strategy_id") or "")
        order_ids = [str(row.get("order_id") or "")]
        if row.get("event_type") == "position_closed":
            entry = payload.get("entry_order_id")
            exit_ = payload.get("exit_order_id")
            if entry and exit_:
                exits.setdefault(str(entry), set()).add(str(exit_))
            order_ids.append(str(exit_ or ""))
        for order_id in order_ids:
            if not order_id or order_id.startswith("reconcile-"):
                continue
            if not strategy or strategy == "unknown":
                continue
            known = order_strategy.get(order_id)
            if known is not None and known != strategy:
                conflicts.add(order_id)
            order_strategy.setdefault(order_id, strategy)
    for order_id in conflicts:
        order_strategy.pop(order_id, None)
    return order_strategy, exits, symbols, sorted(conflicts)


async def compute_ground_truth(
    db_manager: Any,
    window_days: float,
    client: ExchangeHistoryClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read every input for the window and build the report."""
    mongodb = getattr(db_manager, "mongodb_adapter", None)
    mysql = getattr(db_manager, "mysql_adapter", None)
    if mongodb is None or mysql is None:
        raise GroundTruthInputError("MongoDB and MySQL are both required")
    end = now or datetime.now(UTC)
    start = end - timedelta(days=window_days)
    client = client or ExchangeHistoryClient()

    rows = await read_position_rows(mongodb, start, end)
    order_strategy, _journal_exits, symbols, conflicts = await asyncio.to_thread(
        read_order_journal, mysql, start, end
    )
    symbols |= {str(r["symbol"]) for r in rows if r.get("symbol")}

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    fills: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for symbol in sorted(symbols):
        fills += await client.fills(symbol, start_ms, end_ms)
        orders += await client.orders(symbol, start_ms, end_ms)

    # The ledger's entry orders are a second source for order -> strategy; the
    # journal wins, and an order claimed by two strategies stays unmapped.
    for row in rows:
        order_id = str(row.get("entry_order_id") or "")
        strategy = str(row.get("strategy_id") or "")
        if order_id and strategy and order_id not in conflicts:
            known = order_strategy.setdefault(order_id, strategy)
            if known != strategy:
                conflicts.append(order_id)
                order_strategy.pop(order_id, None)
    report = build_ground_truth_v2(fills, orders, order_strategy, rows)
    report["metadata"] = {
        "calculated_at": datetime.now(UTC).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "symbols": sorted(symbols),
        "position_rows": len(rows),
        "orders": len(orders),
        "order_strategy_conflicts": sorted(set(conflicts)),
        "method": "exchange flat-to-flat episodes (v2)",
        "source": "binance-testnet-userTrades-via-tradeengine",
    }
    return report
