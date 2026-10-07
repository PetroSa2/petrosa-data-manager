"""Read-only slippage-per-regime report from ``execution_events`` (petrosa-data-manager#535).

    python -m data_manager.maintenance.slippage_report --days 30 [--symbol ETHUSDT] [--role entry]

Prints a short text summary and the report as JSON. It never writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from data_manager.db.database_manager import DatabaseManager
from data_manager.services.slippage_report import (
    FILL_EVENT_TYPES,
    build_report,
    summary_lines,
)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manager = DatabaseManager()
    await manager.initialize()
    try:
        db = manager.mongodb_adapter.db
        query: dict[str, Any] = {
            "event_type": {"$in": sorted(FILL_EVENT_TYPES)},
            "timestamp": {"$gte": datetime.now(UTC) - timedelta(days=args.days)},
        }
        if args.symbol:
            query["symbol"] = args.symbol
        fills = (
            await db["execution_events"]
            .find(query)
            .sort("timestamp", 1)
            .to_list(length=None)
        )
        regimes: dict[str, list[dict[str, Any]]] = {}
        for pair in sorted(
            {str(row.get("symbol")) for row in fills if row.get("symbol")}
        ):
            regimes[pair] = (
                await db[f"analytics_{pair}_regime"].find({}).to_list(length=None)
            )
        return build_report(fills, regimes, role=args.role)
    finally:
        await manager.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days", type=float, default=30.0, help="trailing window of fills"
    )
    parser.add_argument("--symbol", help="one symbol; all when omitted")
    parser.add_argument(
        "--role", choices=("entry", "exit"), help="entry or exit fills only"
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run(args))
    print("\n".join(summary_lines(report)))
    print(json.dumps(report, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
