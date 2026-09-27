"""Copy legacy strategy configuration documents into the split collections."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

import constants


def _sort_key(document: dict[str, Any]) -> tuple[int, datetime]:
    updated_at = document.get("updated_at")
    if not isinstance(updated_at, datetime):
        updated_at = datetime.min
    elif updated_at.tzinfo is not None:
        updated_at = updated_at.astimezone(UTC).replace(tzinfo=None)
    return int(document.get("version", 0)), updated_at


def preferred_document(
    existing: dict[str, Any] | None, candidate: dict[str, Any]
) -> tuple[dict[str, Any], bool, bool]:
    """Return the winning document and whether it should be written."""
    if existing is None:
        return candidate, True, False
    if _sort_key(candidate) > _sort_key(existing):
        return candidate, True, True
    return existing, False, True


async def _copy_collection(
    source_collection: Any,
    target_collection: Any,
    *,
    key_fields: tuple[str, ...],
    dry_run: bool,
    normalizer: Any = None,
) -> dict[str, int]:
    counts = {"copied": 0, "skipped_older": 0, "conflicts": 0}
    async for raw_document in source_collection.find({}):
        document = normalizer(raw_document) if normalizer else dict(raw_document)
        key = {field: document.get(field) for field in key_fields}
        existing = await target_collection.find_one(key)
        winner, should_copy, conflict = preferred_document(existing, document)
        if conflict:
            counts["conflicts"] += 1
        if not should_copy:
            counts["skipped_older"] += 1
            continue
        counts["copied"] += 1
        if not dry_run:
            await target_collection.replace_one(key, winner, upsert=True)
    return counts


def _normalize_legacy(document: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(document)
    normalized.setdefault("symbol", None)
    normalized.setdefault("side", None)
    normalized.pop("_id", None)
    return normalized


async def _copy_legacy_documents(
    source_collection: Any,
    target_db: Any,
    *,
    dry_run: bool,
) -> dict[str, int]:
    counts = {"copied": 0, "skipped_older": 0, "conflicts": 0}
    async for raw_document in source_collection.find({}):
        document = _normalize_legacy(raw_document)
        collection = (
            target_db["strategy_configs_global"]
            if document["symbol"] is None
            else target_db["strategy_configs_symbol"]
        )
        key_fields = (
            ("strategy_id",)
            if document["symbol"] is None
            else (
                "strategy_id",
                "symbol",
                "side",
            )
        )
        key = {field: document.get(field) for field in key_fields}
        existing = await collection.find_one(key)
        winner, should_copy, conflict = preferred_document(existing, document)
        if conflict:
            counts["conflicts"] += 1
        if not should_copy:
            counts["skipped_older"] += 1
            continue
        counts["copied"] += 1
        if not dry_run:
            await collection.replace_one(key, winner, upsert=True)
    return counts


async def copy_strategy_configs(
    client: Any,
    *,
    source_db_name: str = "petrosa",
    target_db_name: str = constants.CANDLE_MONGO_DATABASE,
    dry_run: bool = True,
) -> dict[str, dict[str, int]]:
    """Copy source split/legacy configs and deduplicated audit rows."""
    source = client[source_db_name]
    target = client[target_db_name]
    results: dict[str, dict[str, int]] = {}
    results["strategy_configs_global"] = await _copy_collection(
        source["strategy_configs_global"],
        target["strategy_configs_global"],
        key_fields=("strategy_id",),
        dry_run=dry_run,
    )
    results["strategy_configs_symbol"] = await _copy_collection(
        source["strategy_configs_symbol"],
        target["strategy_configs_symbol"],
        key_fields=("strategy_id", "symbol", "side"),
        dry_run=dry_run,
    )
    results["strategy_configs_legacy"] = await _copy_legacy_documents(
        target["strategy_configs"], target, dry_run=dry_run
    )

    audit_counts = {"copied": 0, "skipped_older": 0, "conflicts": 0}
    async for document in source["strategy_config_audit"].find({}):
        audit_id = document.get("_id")
        existing = await target["strategy_config_audit"].find_one({"_id": audit_id})
        if existing is not None:
            audit_counts["skipped_older"] += 1
            continue
        audit_counts["copied"] += 1
        if not dry_run:
            await target["strategy_config_audit"].insert_one(document)
    results["strategy_config_audit"] = audit_counts
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--source-db", default="petrosa")
    return parser


async def _main(args: argparse.Namespace) -> None:
    client = AsyncIOMotorClient(constants.MONGODB_URL)
    try:
        results = await copy_strategy_configs(
            client, source_db_name=args.source_db, dry_run=not args.apply
        )
        for collection, counts in results.items():
            print(
                f"{collection}: copied={counts['copied']} "
                f"skipped_older={counts['skipped_older']} conflicts={counts['conflicts']}"
            )
    finally:
        client.close()


def main() -> None:
    asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    main()
