"""Queue positions whose Mongo fee or decision fields are absent in MySQL.

Dry run is the default. Apply mode uses bounded batches and the repository's
versioned empty update, so the normal copy worker writes the complete Mongo
document without changing business fields.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import constants
from data_manager.db.repositories.trading_state_repository import TradingStateRepository

FIELDS = ("fee_status", "decision_id")


async def candidates(
    mongo_db: Any, mysql_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return Mongo positions with a value missing from the corresponding MySQL row."""
    mysql_by_id = {
        row["position_id"]: row for row in mysql_rows if row.get("position_id")
    }
    rows = (
        await mongo_db["positions"]
        .find({"position_id": {"$in": list(mysql_by_id)}})
        .sort([("_id", 1)])
        .to_list(length=None)
    )
    return [
        row
        for row in rows
        if any(
            row.get(field) is not None
            and mysql_by_id[row["position_id"]].get(field) is None
            for field in FIELDS
        )
    ]


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
            if await repo.update_position(row["position_id"], {}):
                queued += 1
            else:
                skipped += 1
    return {"queued": queued, "skipped": skipped}


def _mysql_rows() -> list[dict[str, Any]]:
    from data_manager.db.mysql_adapter import MySQLAdapter

    mysql = MySQLAdapter(constants.MYSQL_URI, role="adhoc")
    mysql.connect()
    try:
        rows, _ = mysql.find_paginated(
            "positions",
            columns=["position_id", *FIELDS],
            limit=1_000_000,
            include_total=False,
        )
        return rows
    finally:
        mysql.disconnect()


async def run(*, apply: bool, batch: int, pause_seconds: float) -> dict[str, Any]:
    from data_manager.db.mongodb_adapter import MongoDBAdapter

    mongo = MongoDBAdapter(
        constants.MONGODB_URL, database_name=constants.CANDLE_MONGO_DATABASE
    )
    mongo.connect()
    try:
        rows = await candidates(mongo.db, _mysql_rows())
        result: dict[str, Any] = {"apply": apply, "candidates": len(rows)}
        if apply:
            result.update(
                await requeue(
                    TradingStateRepository(None, mongo),
                    rows,
                    batch=batch,
                    pause_seconds=pause_seconds,
                )
            )
        return result
    finally:
        mongo.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--pause", type=float, default=60.0)
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
