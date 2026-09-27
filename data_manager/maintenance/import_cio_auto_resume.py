"""Import a Redis HGETALL JSON export into the CIO auto-resume registry."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import constants
from data_manager.api.routes.cio_state import CioPauseEntry
from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.db.repositories.cio_auto_resume_repository import (
    CioAutoResumeRepository,
)

logger = logging.getLogger(__name__)


def read_entries(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("input must be a JSON object keyed by strategy_id")
    entries = []
    for strategy_id, value in raw.items():
        payload = json.loads(value) if isinstance(value, str) else value
        payload = {"strategy_id": strategy_id, **payload}
        entries.append(CioPauseEntry.model_validate(payload).model_dump())
    return entries


async def import_entries(entries: list[dict[str, Any]], apply: bool) -> int:
    if not apply:
        return len(entries)
    adapter = MongoDBAdapter(connection_string=constants.MONGODB_URL)
    adapter.connect()
    try:
        repo = CioAutoResumeRepository(None, adapter)
        for entry in entries:
            await repo.upsert_entry(entry)
    finally:
        adapter.disconnect()
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-json", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    entries = read_entries(args.from_json)
    count = asyncio.run(import_entries(entries, args.apply))
    print(f"{'imported' if args.apply else 'validated'} {count} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
