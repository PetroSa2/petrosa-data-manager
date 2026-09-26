"""Seed live trading state from MySQL into MongoDB without overwriting Mongo."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta, timezone

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter


async def seed(*, apply: bool) -> dict[str, int]:
    mysql = MySQLAdapter(constants.MYSQL_URI)
    mysql.connect()
    mongo = MongoDBAdapter(
        constants.MONGODB_URL, database_name=constants.CANDLE_MONGO_DATABASE
    )
    mongo.connect()
    positions = mysql.read(
        "positions", filters={"status": {"$in": ["open", "partially_closed"]}}
    )
    since = datetime.now(UTC).date() - timedelta(days=7)
    pnl = mysql.read("daily_pnl", filters={"date": {"$gte": since}})
    if apply:
        for collection, rows, key in (
            ("positions", positions, "position_id"),
            ("daily_pnl", pnl, "date"),
        ):
            for row in rows:
                await mongo.db[collection].update_one(
                    {key: row[key]},
                    {"$setOnInsert": mongo._prepare_for_bson(dict(row))},
                    upsert=True,
                )
    mongo.disconnect()
    mysql.disconnect()
    return {"positions": len(positions), "daily_pnl": len(pnl)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="Report rows without writing (default)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write missing rows to MongoDB"
    )
    args = parser.parse_args()
    print(asyncio.run(seed(apply=args.apply and not args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
