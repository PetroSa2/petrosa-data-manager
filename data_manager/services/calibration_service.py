"""Confidence calibration records built from the immutable audit collections."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from data_manager.services.round_book import FILL_EVENT_TYPES, RoundBook, _when

MAX_ROWS = 50_000
FRESHNESS_MAX_AGE_MINUTES = 30
ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def build_calibration_records(
    execution_events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Join closed round-book entries to executed CIO decisions."""
    book = RoundBook()
    ordered = sorted(
        (row for row in execution_events if row.get("event_type") in FILL_EVENT_TYPES),
        key=lambda row: _when(row) or datetime.min.replace(tzinfo=UTC),
    )
    for row in ordered:
        book.apply(row)

    decisions_by_id = {
        str(row.get("decision_id")): row for row in decisions if row.get("decision_id")
    }
    records: list[dict[str, Any]] = []
    skipped = 0
    for closed in book.closed:
        if since is not None:
            close_time = closed.closed_at
            if close_time < since:
                continue
        decision = None
        confidence = None
        for decision_id in closed.decision_ids:
            candidate = decisions_by_id.get(decision_id)
            if not candidate or str(candidate.get("action", "")).lower() != "execute":
                continue
            candidate_confidence = _decimal(candidate.get("confidence"))
            if (
                candidate_confidence is not None
                and ZERO <= candidate_confidence <= Decimal("1")
            ):
                decision = candidate
                confidence = candidate_confidence
                break
        if confidence is None or not ZERO <= confidence <= Decimal("1"):
            skipped += 1
            continue
        gross = closed.realized_dec
        costs = closed.fees
        records.append(
            {
                "strategy_id": closed.strategy_id,
                "confidence": confidence,
                "net_pnl": gross - costs,
                "gross_pnl": gross,
                "costs": costs,
                "total_costs": costs,
                "decision_id": str(decision["decision_id"]),
                "symbol": closed.symbol,
                "closed_at": closed.closed_at.isoformat(),
            }
        )
    return {"records": records, "skipped": skipped}


async def get_calibration_records(
    mongodb: Any,
    *,
    since: datetime | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    """Read bounded audit data through the database gateway and build records."""
    filters = {"strategy_id": strategy_id} if strategy_id else None
    execution_events = await mongodb.find_filtered(
        "execution_events",
        filters=filters,
        limit=MAX_ROWS,
        sort_order=1,
    )
    decisions = await mongodb.find_filtered(
        "cio_decisions",
        filters={"strategy_id": strategy_id, "action": "execute"},
        limit=MAX_ROWS,
        sort_order=1,
    )
    return build_calibration_records(execution_events, decisions, since=since)


def calibration_freshness(
    records: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return the bounded freshness state for a calibration report."""
    timestamps = [
        datetime.fromisoformat(str(record["closed_at"]))
        for record in records
        if record.get("closed_at")
    ]
    latest_at = max(timestamps) if timestamps else None
    current = now or datetime.now(UTC)
    if latest_at is not None and latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=UTC)
    age_minutes = (
        max(0.0, (current - latest_at).total_seconds() / 60)
        if latest_at is not None
        else None
    )
    return {
        "latest_at": latest_at.isoformat() if latest_at else None,
        "fresh": age_minutes is not None and age_minutes <= FRESHNESS_MAX_AGE_MINUTES,
        "max_age_minutes": FRESHNESS_MAX_AGE_MINUTES,
        "age_minutes": age_minutes,
    }


async def get_latest_calibration(
    mongodb: Any,
    *,
    since: datetime | None = None,
    strategy_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the calibration report and add its operational freshness signal."""
    report = await get_calibration_records(
        mongodb, since=since, strategy_id=strategy_id
    )
    return {**report, **calibration_freshness(report["records"], now=now)}
