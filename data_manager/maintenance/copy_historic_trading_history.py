"""Copy MongoDB trading history into the durable MySQL tables."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import constants
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.mysql_adapter import MySQLAdapter
from data_manager.models.execution_event import ExecutionEvent
from data_manager.models.market_data import TradeFill

COLLECTIONS = ("execution_events", "trades")
MODEL_TYPES = {"execution_events": ExecutionEvent, "trades": TradeFill}


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        numeric: Decimal | None = None
        if not isinstance(value, bool) and isinstance(value, int | float | Decimal):
            numeric = Decimal(str(value))
        elif isinstance(value, str):
            try:
                numeric = Decimal(value)
            except (InvalidOperation, ValueError):
                numeric = None

        if numeric is not None:
            if not numeric.is_finite():
                raise ValueError(f"invalid numeric timestamp: {value!r}")
            if abs(numeric) >= Decimal("1e11"):
                numeric /= Decimal(1000)
            try:
                parsed = datetime.fromtimestamp(float(numeric), UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise ValueError(f"invalid epoch timestamp: {value!r}") from exc
        else:
            timestamp = str(value)
            if timestamp.endswith("Z"):
                if re.search(r"[+-]\d{2}:\d{2}Z$", timestamp):
                    timestamp = timestamp[:-1]
                else:
                    timestamp = f"{timestamp[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid timestamp: {value!r}") from exc
    try:
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc


def _collection_names(names: list[str], requested: str | None) -> list[str]:
    selected = [
        name
        for name in names
        if name == "execution_events" or name == "trades" or name.startswith("trades_")
    ]
    if requested is None:
        return sorted(selected)
    if requested not in selected:
        raise ValueError(f"copy collection not found: {requested}")
    return [requested]


def _checkpoint(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("checkpoint must contain an object")
    return {str(key): str(timestamp) for key, timestamp in value.items()}


def _save_checkpoint(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(values, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _model(collection: str, document: dict[str, Any]) -> Any:
    value = {key: item for key, item in document.items() if key != "_id"}
    value["timestamp"] = _timestamp(value["timestamp"])
    if collection != "execution_events":
        order_id = value["order_id"]
        if isinstance(order_id, int | float | Decimal) and not isinstance(
            order_id, bool
        ):
            if not Decimal(str(order_id)).is_finite():
                raise ValueError(f"invalid order ID: {order_id!r}")
            value["order_id"] = str(order_id)
        value["trade_time"] = _timestamp(value["trade_time"])
        value["extracted_at"] = _timestamp(value["extracted_at"])
    return MODEL_TYPES[
        "execution_events" if collection == "execution_events" else "trades"
    ](**value)


async def copy_collection(
    mongo_collection: Any,
    mysql: Any,
    collection: str,
    *,
    batch_size: int,
    apply: bool,
    checkpoint: dict[str, str],
    checkpoint_path: Path | None,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    lower = since
    saved = checkpoint.get(collection)
    if saved:
        lower = max(lower, _timestamp(saved)) if lower else _timestamp(saved)
    if lower or until:
        query["timestamp"] = {}
        if lower:
            query["timestamp"]["$gt"] = lower
        if until:
            query["timestamp"]["$lt"] = until
    cursor = mongo_collection.find(query).sort("timestamp", 1)
    counts: Counter[str] = Counter()
    invalid = 0
    copied = 0
    while True:
        documents = await cursor.to_list(length=batch_size)
        if not documents:
            break
        models = []
        last_timestamp: datetime | None = None
        for document in documents:
            try:
                model = _model(collection, dict(document))
            except (KeyError, TypeError, ValueError):
                invalid += 1
                continue
            models.append(model)
            last_timestamp = model.timestamp
            counts[model.timestamp.astimezone(UTC).date().isoformat()] += 1
        if models and apply:
            mysql.write_batch(
                models,
                "execution_events" if collection == "execution_events" else "trades",
                batch_size,
                insert_only=True,
            )
            if checkpoint_path and last_timestamp:
                checkpoint[collection] = last_timestamp.isoformat()
                _save_checkpoint(checkpoint_path, checkpoint)
        copied += len(models)
    return {
        "collection": collection,
        "rows": copied,
        "invalid": invalid,
        "days": dict(sorted(counts.items())),
    }


async def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    mongo = MongoDBAdapter(
        constants.MONGODB_URL, database_name=constants.CANDLE_MONGO_DATABASE
    )
    mysql = MySQLAdapter(constants.MYSQL_URI)
    mongo.connect()
    mysql.connect()
    try:
        names = await mongo.list_collections()
        selected = _collection_names(names, args.collection)
        if not selected:
            raise RuntimeError("no execution_events or trades collections found")
        checkpoint = _checkpoint(args.checkpoint)
        since = _timestamp(args.since) if args.since else None
        until = _timestamp(args.until) if args.until else None
        results = []
        for name in selected:
            results.append(
                await copy_collection(
                    mongo.db[name],
                    mysql,
                    name,
                    batch_size=args.batch_size,
                    apply=args.apply,
                    checkpoint=checkpoint,
                    checkpoint_path=args.checkpoint,
                    since=since,
                    until=until,
                )
            )
        return results
    finally:
        mongo.disconnect()
        mysql.disconnect()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", choices=COLLECTIONS)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    results = asyncio.run(run(_parse_args(argv)))
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
