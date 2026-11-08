"""Queue Mongo positions that never entered the MySQL copy (PetroSa2/petrosa-data-manager#881).

Positions written before the trading-state copy existed (#464, 2026-10-23) have no copy markers, so nothing ever queued
them, and they are missing from the MySQL history (603 on 2026-11-07). This command finds Mongo positions that have no
MySQL row, no ``_mysql_copy_version`` and no quarantine, and queues each through the repository's versioned write (an
empty update: it bumps the copy version and marks it pending without changing any field). The running copy worker then
inserts them, version-gated as usual. Quarantined positions are left to the copy loop's release rule.

Dry run by default: prints counts only. ``--apply`` queues in batches of ``--batch`` (default 100) with ``--pause``
seconds (default 60) between batches, so the worker drains each batch before the next. Rerunnable: queued positions
carry copy markers and are no longer candidates.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
from typing import Any

import constants
from data_manager.db.repositories.trading_state_repository import (
    TradingStateRepository,
)

CANDIDATE_FILTER: dict[str, Any] = {
    "_mysql_copy_version": {"$exists": False},
    "_mysql_copy_quarantined": {"$ne": True},
}


async def candidates(
    mongo_db: Any, mysql_position_ids: set[str]
) -> list[dict[str, Any]]:
    """Mongo positions with no MySQL row and no copy markers, oldest first."""
    rows = await (
        mongo_db["positions"]
        .find(
            CANDIDATE_FILTER,
            {"position_id": 1, "status": 1, "created_at": 1, "updated_at": 1},
        )
        .sort([("_id", 1)])
        .to_list(length=None)
    )
    return [
        row
        for row in rows
        if row.get("position_id") and row["position_id"] not in mysql_position_ids
    ]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": len(rows),
        "by_status": dict(collections.Counter(row.get("status") for row in rows)),
        "by_created_month": dict(
            collections.Counter(str(row.get("created_at"))[:7] for row in rows)
        ),
    }


async def requeue(
    repo: TradingStateRepository,
    rows: list[dict[str, Any]],
    *,
    batch: int,
    pause_seconds: float,
) -> dict[str, int]:
    queued = skipped = 0
    for start in range(0, len(rows), batch):
        if start:
            await asyncio.sleep(pause_seconds)
        for row in rows[start : start + batch]:
            changed = await repo.update_position(row["position_id"], {})
            if changed:
                queued += 1
            else:
                skipped += 1
        print(
            json.dumps(
                {
                    "batch_end": min(start + batch, len(rows)),
                    "queued": queued,
                    "skipped": skipped,
                }
            ),
            flush=True,
        )
    return {"queued": queued, "skipped": skipped}


def _mysql_position_ids() -> set[str]:
    from data_manager.db.mysql_adapter import MySQLAdapter

    mysql = MySQLAdapter(constants.MYSQL_URI, role="adhoc")
    mysql.connect()
    try:
        rows, _ = mysql.find_paginated(
            "positions", columns=["position_id"], limit=1_000_000, include_total=False
        )
        return {row["position_id"] for row in rows if row.get("position_id")}
    finally:
        mysql.disconnect()


async def run(*, apply: bool, batch: int, pause_seconds: float) -> dict[str, Any]:
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    mongo = MongoDBAdapter(
        constants.MONGODB_URL, database_name=constants.CANDLE_MONGO_DATABASE
    )
    mongo.connect()
    try:
        rows = await candidates(mongo.db, _mysql_position_ids())
        result: dict[str, Any] = {"apply": apply, **summary(rows)}
        if apply:
            repo = TradingStateRepository(None, mongo)
            result.update(
                await requeue(repo, rows, batch=batch, pause_seconds=pause_seconds)
            )
        return result
    finally:
        mongo.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="Queue the candidates (default: dry run)"
    )
    parser.add_argument(
        "--batch", type=int, default=100, help="Positions queued per batch"
    )
    parser.add_argument(
        "--pause", type=float, default=60.0, help="Seconds between batches"
    )
    args = parser.parse_args(argv)
    if args.batch < 1:
        parser.error("--batch must be at least 1")
    print(
        json.dumps(
            asyncio.run(
                run(apply=args.apply, batch=args.batch, pause_seconds=args.pause)
            ),
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
