"""Evaluate the per-day Mongo to MySQL historic-copy safety proof."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter


@dataclass(frozen=True)
class CopyProofResult:
    collection: str
    checked_days: tuple[date, ...]
    failures: tuple[str, ...]
    generated_at: datetime
    oldest_mongo_timestamp: datetime | None = None
    invalid_timestamp_count: int = 0

    @property
    def proven(self) -> bool:
        return (
            bool(self.checked_days)
            and not self.failures
            and self.oldest_mongo_timestamp is not None
            and self.invalid_timestamp_count == 0
        )


def is_proof_collection(name: str) -> bool:
    """Return whether a Mongo collection requires a historic-copy proof."""
    return name == "execution_events" or name == "trades" or name.startswith("trades_")


def prove_daily_copy(
    collection: str,
    mongo_counts: Mapping[date, int],
    mysql_counts: Mapping[date, int],
    *,
    now: datetime | None = None,
    retention_days: int,
    copy_lag: timedelta,
    oldest_mongo_timestamp: datetime | None = None,
    invalid_timestamp_count: int = 0,
) -> CopyProofResult:
    """Require MySQL to contain at least as many rows for every complete UTC day."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    if copy_lag < timedelta(0):
        raise ValueError("copy_lag must not be negative")
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    if invalid_timestamp_count < 0:
        raise ValueError("invalid_timestamp_count must not be negative")
    deletion_window = max(timedelta(days=retention_days), copy_lag)
    last_day = (generated_at - deletion_window).date()
    if oldest_mongo_timestamp is None:
        first_day = last_day - timedelta(days=retention_days - 1)
    else:
        first_day = oldest_mongo_timestamp.astimezone(UTC).date()
    days = (
        tuple(first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1))
        if first_day <= last_day
        else ()
    )
    failures: list[str] = []
    if invalid_timestamp_count:
        failures.append(f"invalid timestamps: {invalid_timestamp_count}")
    for day in days:
        mongo_count = int(mongo_counts.get(day, 0))
        mysql_count = int(mysql_counts.get(day, 0))
        if mongo_count > mysql_count:
            failures.append(
                f"{day.isoformat()}: mongo={mongo_count} mysql={mysql_count}"
            )
    return CopyProofResult(
        collection=collection,
        checked_days=days,
        failures=tuple(failures),
        generated_at=generated_at,
        oldest_mongo_timestamp=oldest_mongo_timestamp
        or datetime.combine(first_day, datetime.min.time(), tzinfo=UTC),
        invalid_timestamp_count=invalid_timestamp_count,
    )


async def _mongo_timestamp_metadata(
    database: Any, collection: str
) -> tuple[datetime | None, int]:
    cursor = database[collection].aggregate(
        [
            {
                "$project": {
                    "normalized": {
                        "$convert": {
                            "input": "$timestamp",
                            "to": "date",
                            "onError": None,
                            "onNull": None,
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "oldest": {"$min": "$normalized"},
                    "invalid": {
                        "$sum": {
                            "$cond": [{"$eq": ["$normalized", None]}, 1, 0]
                        }
                    },
                }
            },
        ]
    )
    rows = await cursor.to_list(length=None)
    if not rows:
        return None, 0
    oldest = rows[0].get("oldest")
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return oldest, int(rows[0].get("invalid", 0))


async def _mongo_daily_counts(
    database: Any,
    collection: str,
    *,
    start: datetime,
    end: datetime,
) -> dict[date, int]:
    cursor = database[collection].aggregate(
        [
            {
                "$project": {
                    "normalized": {
                        "$convert": {
                            "input": "$timestamp",
                            "to": "date",
                            "onError": None,
                            "onNull": None,
                        }
                    }
                }
            },
            {"$match": {"normalized": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$normalized",
                            "timezone": "UTC",
                        }
                    },
                    "count": {"$sum": 1},
                }
            },
        ]
    )
    rows = await cursor.to_list(length=None)
    return {date.fromisoformat(row["_id"]): int(row["count"]) for row in rows}


def _mysql_daily_counts(
    adapter: MySQLAdapter,
    table_name: str,
    *,
    start: datetime,
    end: datetime,
) -> dict[date, int]:
    if adapter.engine is None:
        raise RuntimeError("MySQL adapter is not connected")
    query = sa.text(
        f"SELECT DATE(timestamp) AS day, COUNT(*) AS count FROM `{table_name}` "
        "WHERE timestamp >= :start AND timestamp < :end GROUP BY DATE(timestamp)"
    )
    with adapter.engine.connect() as connection:
        rows = connection.execute(query, {"start": start, "end": end}).mappings()
        return {
            value.date() if isinstance(value, datetime) else value: int(row["count"])
            for row in rows
            for value in [row["day"]]
        }


async def _run_cli(
    collection: str | None, retention_days: int, copy_lag_seconds: int
) -> None:
    mongo = MongoDBAdapter(
        constants.MONGODB_URL,
        database_name=constants.CANDLE_MONGO_DATABASE,
    )
    mysql = MySQLAdapter(constants.MYSQL_URI)
    mongo.connect()
    mysql.connect()
    try:
        names = await mongo.list_collections()
        selected = sorted(name for name in names if is_proof_collection(name))
        if collection is not None:
            if collection not in selected:
                raise ValueError(f"proof collection not found: {collection}")
            selected = [collection]
        if not selected:
            raise RuntimeError(
                "no historic-copy proof collections found in the configured Mongo database"
            )
        now = datetime.now(UTC)
        lag = timedelta(seconds=copy_lag_seconds)
        deletion_window = max(timedelta(days=retention_days), lag)
        last_day = (now - deletion_window).date()
        metadata = {
            member: await _mongo_timestamp_metadata(mongo.db, member)
            for member in selected
        }
        usable_oldest = [oldest for oldest, _ in metadata.values() if oldest is not None]
        if not usable_oldest:
            raise RuntimeError("no usable Mongo timestamps found in proof collections")
        first_day = min(usable_oldest).date()
        start = datetime.combine(first_day, datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(
            last_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        groups = (
            [("execution_events", ["execution_events"])]
            if collection == "execution_events"
            else [(collection, [collection])]
            if collection is not None
            else [
                ("execution_events", ["execution_events"])
                if "execution_events" in selected
                else None,
                ("trades", [name for name in selected if name != "execution_events"])
                if any(name != "execution_events" for name in selected)
                else None,
            ]
        )
        results = []
        for group in groups:
            if group is None:
                continue
            name, members = group
            mongo_counts: dict[date, int] = {}
            oldest = None
            invalid = 0
            for member in members:
                member_oldest, member_invalid = metadata[member]
                if member_oldest is not None:
                    oldest = member_oldest if oldest is None else min(oldest, member_oldest)
                invalid += member_invalid
                counts = await _mongo_daily_counts(
                    mongo.db, member, start=start, end=end
                )
                for day, count in counts.items():
                    mongo_counts[day] = mongo_counts.get(day, 0) + count
            mysql_counts = _mysql_daily_counts(
                mysql,
                "execution_events" if name == "execution_events" else "trades",
                start=start,
                end=end,
            )
            results.append(
                prove_daily_copy(
                    name,
                    mongo_counts,
                    mysql_counts,
                    now=now,
                    retention_days=retention_days,
                    copy_lag=lag,
                    oldest_mongo_timestamp=oldest,
                    invalid_timestamp_count=invalid,
                ).__dict__
            )
        print(json.dumps(results, default=str))
    finally:
        mongo.disconnect()
        mysql.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection")
    parser.add_argument(
        "--retention-days", type=int, default=constants.TRADES_RETENTION_DAYS
    )
    parser.add_argument(
        "--copy-lag-seconds", type=int, default=constants.HISTORIC_COPY_LAG_SECONDS
    )
    args = parser.parse_args()
    asyncio.run(_run_cli(args.collection, args.retention_days, args.copy_lag_seconds))


if __name__ == "__main__":
    main()
