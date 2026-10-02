"""Bounded conversion of legacy Mongo timestamp strings to BSON dates."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from pymongo import ASCENDING

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter


@dataclass(frozen=True)
class MigrationResult:
    collection: str
    scanned: int
    converted: int
    invalid: int
    dry_run: bool
    batches: int

    @property
    def complete(self) -> bool:
        return self.invalid == 0


async def migrate_collection(
    collection: Any,
    collection_name: str,
    *,
    batch_size: int = 1000,
    dry_run: bool = True,
    max_batches: int | None = None,
) -> MigrationResult:
    """Convert string timestamps in bounded `_id`-ordered batches."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    last_id: Any = None
    scanned = converted = invalid = batches = 0
    while max_batches is None or batches < max_batches:
        query: dict[str, Any] = {"timestamp": {"$type": "string"}}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        cursor = collection.find(query).sort("_id", ASCENDING).limit(batch_size)
        rows = await cursor.to_list(length=batch_size)
        if not rows:
            break
        batches += 1
        for row in rows:
            scanned += 1
            last_id = row["_id"]
            raw = row.get("timestamp")
            try:
                parsed = MongoDBAdapter._as_utc_datetime(raw)
            except (TypeError, ValueError):
                invalid += 1
                continue
            converted += 1
            if not dry_run:
                await collection.update_one(
                    {"_id": row["_id"], "timestamp": raw},
                    {"$set": {"timestamp": parsed}},
                )

        if len(rows) < batch_size:
            break

    return MigrationResult(
        collection=collection_name,
        scanned=scanned,
        converted=converted,
        invalid=invalid,
        dry_run=dry_run,
        batches=batches,
    )


async def migrate_collections(
    database: Any,
    collection_names: list[str],
    *,
    batch_size: int = 1000,
    dry_run: bool = True,
    max_batches: int | None = None,
) -> list[MigrationResult]:
    """Run the bounded migration for the selected collections."""
    return [
        await migrate_collection(
            database[name],
            name,
            batch_size=batch_size,
            dry_run=dry_run,
            max_batches=max_batches,
        )
        for name in collection_names
    ]


async def _run_cli(apply: bool, batch_size: int) -> None:
    adapter = MongoDBAdapter(
        constants.MONGODB_URL,
        database_name=constants.MONGODB_DB,
    )
    adapter.connect()
    try:
        names = await adapter.list_collections()
        selected = sorted(
            name for name in names if name.startswith(("klines_", "trades_"))
        )
        results = await migrate_collections(
            adapter.db,
            selected,
            batch_size=batch_size,
            dry_run=not apply,
        )
        print(json.dumps([result.__dict__ for result in results], default=str))
    finally:
        adapter.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    asyncio.run(_run_cli(args.apply, args.batch_size))


if __name__ == "__main__":
    main()
