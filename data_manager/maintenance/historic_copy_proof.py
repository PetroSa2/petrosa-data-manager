"""Evaluate the per-day Mongo to MySQL historic-copy safety proof."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


@dataclass(frozen=True)
class CopyProofResult:
    collection: str
    checked_days: tuple[date, ...]
    failures: tuple[str, ...]
    generated_at: datetime

    @property
    def proven(self) -> bool:
        return bool(self.checked_days) and not self.failures


def prove_daily_copy(
    collection: str,
    mongo_counts: Mapping[date, int],
    mysql_counts: Mapping[date, int],
    *,
    now: datetime | None = None,
    retention_days: int,
    copy_lag: timedelta,
) -> CopyProofResult:
    """Require MySQL to contain at least as many rows for every complete UTC day."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    if copy_lag < timedelta(0):
        raise ValueError("copy_lag must not be negative")
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    last_day = (generated_at - copy_lag).date()
    first_day = last_day - timedelta(days=retention_days - 1)
    days = tuple(first_day + timedelta(days=offset) for offset in range(retention_days))
    failures: list[str] = []
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
    )
