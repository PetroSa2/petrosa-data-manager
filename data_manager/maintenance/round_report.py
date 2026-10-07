"""Read-only per-strategy fill and round report from ``execution_events`` (petrosa-data-manager#537).

    python -m data_manager.maintenance.round_report --days 30 [--strategy iceberg_detector]

Prints, per strategy, the fills, entry and exit fills, closed and open rounds, the closed-round rate and the
median holding time, and the fills that belong to no strategy with their reason. It never writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from data_manager.db.database_manager import DatabaseManager
from data_manager.services.round_book import FILL_EVENT_TYPES, build_report


def _line(strategy_id: str, stats: dict[str, Any]) -> str:
    holding = stats["median_holding_seconds"]
    return (
        f"{strategy_id}: fills={stats['fills']} (entry={stats['entry_fills']} exit={stats['exit_fills']}) "
        f"closed_rounds={stats['closed_rounds']} open_rounds={stats['open_rounds']} "
        f"rate/day={stats['closed_round_rate_per_day']:.3f} "
        f"median_holding_s={'-' if holding is None else round(holding)} n={stats['n']}"
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manager = DatabaseManager()
    await manager.initialize()
    try:
        query: dict[str, Any] = {"event_type": {"$in": sorted(FILL_EVENT_TYPES)}}
        if args.strategy:
            query["strategy_id"] = args.strategy
        cursor = (
            manager.mongodb_adapter.db["execution_events"]
            .find(query)
            .sort("timestamp", 1)
        )
        rows = await cursor.to_list(length=None)
        return build_report(rows, window_days=args.days)
    finally:
        await manager.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days", type=float, default=30.0, help="window of the rate and holding time"
    )
    parser.add_argument("--strategy", help="one strategy; all when omitted")
    args = parser.parse_args(argv)
    report = asyncio.run(run(args))
    for strategy_id, stats in report["strategies"].items():
        print(_line(strategy_id, stats))
    print(f"unattributed fills: {report['unattributed'] or 'none'}")
    print(
        "fills netted without a position side: "
        f"{report['totals']['position_side_unknown']}"
    )
    print(json.dumps(report, default=str, sort_keys=True))
    return 0 if report["accounted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
