"""Copy extractor runtime configuration into the data-manager service API store."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

import constants

SERVICE = "binance-data-extractor"


async def copy_extractor_config(
    client: Any, *, dry_run: bool = True, source_db_name: str = "petrosa_config"
) -> dict[str, int]:
    source = client[source_db_name]["data_extractor_config"]
    target = client[constants.CANDLE_MONGO_DATABASE]["service_configs"]
    counts = {"copied": 0, "skipped": 0}
    async for source_doc in source.find({}):
        key = source_doc.get("key") or source_doc.get("name")
        if not key:
            continue
        if await target.find_one({"service": SERVICE, "key": key}):
            counts["skipped"] += 1
            continue
        document = {
            "service": SERVICE,
            "key": key,
            "value": source_doc.get("value"),
            "version": 1,
            "changed_by": source_doc.get("changed_by", "migration"),
            "reason": source_doc.get("reason", "initial extractor config migration"),
            "updated_at": source_doc.get("updated_at", datetime.now(UTC)),
        }
        counts["copied"] += 1
        if not dry_run:
            await target.insert_one(document)
    return counts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--source-db", default="petrosa_config")
    return parser


async def _main(args: argparse.Namespace) -> None:
    client = AsyncIOMotorClient(constants.MONGODB_URL)
    try:
        result = await copy_extractor_config(
            client, dry_run=not args.apply, source_db_name=args.source_db
        )
        print(f"copied={result['copied']} skipped={result['skipped']}")
    finally:
        client.close()


def main() -> None:
    asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    main()
