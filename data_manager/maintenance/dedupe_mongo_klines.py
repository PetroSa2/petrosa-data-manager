"""Dry-run-first cleanup for duplicate MongoDB kline natural keys."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter


@dataclass(frozen=True)
class DedupeResult:
    collection: str
    day: str
    duplicate_keys: int
    duplicate_documents: int
    deleted: int
    dry_run: bool


def _day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date().isoformat()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return "unknown"


def _extracted_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def choose_survivor(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the exchange extract, then newest extraction, then stable id."""
    return max(
        documents,
        key=lambda doc: (
            doc.get("source") == "binance-futures",
            _extracted_at(doc.get("extracted_at")),
            str(doc.get("_id", "")),
        ),
    )


async def dedupe_collection(
    collection: Any,
    collection_name: str,
    *,
    dry_run: bool = True,
) -> list[DedupeResult]:
    """Remove duplicate natural keys and return counts grouped by UTC day."""
    groups = await collection.aggregate(
        [
            {
                "$group": {
                    "_id": {"symbol": "$symbol", "timestamp": "$timestamp"},
                    "count": {"$sum": 1},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
        ]
    ).to_list(length=None)
    by_day: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
    for group in groups:
        key = group["_id"]
        documents = await collection.find(
            {"symbol": key["symbol"], "timestamp": key["timestamp"]}
        ).to_list(length=None)
        if len(documents) >= 2:
            by_day[_day(key["timestamp"])].append(documents)

    results: list[DedupeResult] = []
    for day, duplicate_groups in sorted(by_day.items()):
        duplicate_documents = sum(len(documents) - 1 for documents in duplicate_groups)
        deleted = 0
        if not dry_run:
            for documents in duplicate_groups:
                survivor = choose_survivor(documents)
                losers = [doc["_id"] for doc in documents if doc["_id"] != survivor["_id"]]
                if losers:
                    deleted += (await collection.delete_many({"_id": {"$in": losers}})).deleted_count
        results.append(
            DedupeResult(
                collection=collection_name,
                day=day,
                duplicate_keys=len(duplicate_groups),
                duplicate_documents=duplicate_documents,
                deleted=deleted,
                dry_run=dry_run,
            )
        )
    return results


async def _run_cli(apply: bool, collection: str | None) -> None:
    adapter = MongoDBAdapter(
        constants.MONGODB_URL,
        database_name=constants.CANDLE_MONGO_DATABASE,
    )
    adapter.connect()
    try:
        names = sorted(
            name for name in await adapter.list_collections() if name.startswith("klines_")
        )
        if collection is not None:
            if collection not in names:
                raise ValueError(f"dedupe collection not found: {collection}")
            names = [collection]
        if not names:
            raise RuntimeError("no klines collections found in the configured Mongo database")
        results = []
        for name in names:
            results.extend(await dedupe_collection(adapter.db[name], name, dry_run=not apply))
        print(json.dumps([result.__dict__ for result in results]))
    finally:
        adapter.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--collection")
    args = parser.parse_args()
    asyncio.run(_run_cli(args.apply, args.collection))


if __name__ == "__main__":
    main()
