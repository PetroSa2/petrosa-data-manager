"""Confidence calibration records built from the immutable audit collections."""

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, cast

from sqlalchemy import Column, DateTime, MetaData, Numeric, String, Table, select

from data_manager.db.engine_factory import create_read_only_engine, mark_engine_closing
from data_manager.services.report_precompute import record_report_stage
from data_manager.services.round_book import FILL_EVENT_TYPES, RoundBook, _when

MAX_ROWS = 50_000
MYSQL_DECISION_BATCH_SIZE = 1_000
FRESHNESS_MAX_AGE_MINUTES = 30
ZERO = Decimal("0")
logger = logging.getLogger(__name__)


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
    history_checked: bool = False,
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
    skipped_reasons: Counter[str] = Counter()
    for closed in book.closed:
        if since is not None:
            close_time = closed.closed_at
            if close_time < since:
                continue
        decision = None
        confidence = None
        matched_decisions = 0
        execute_decisions = 0
        missing_confidence = False
        invalid_confidence = False
        for decision_id in closed.decision_ids:
            candidate = decisions_by_id.get(decision_id)
            if not candidate:
                continue
            matched_decisions += 1
            if str(candidate.get("action", "")).lower() != "execute":
                continue
            execute_decisions += 1
            candidate_confidence = _decimal(candidate.get("confidence"))
            if candidate_confidence is None:
                missing_confidence = True
                continue
            if not ZERO <= candidate_confidence <= Decimal("1"):
                invalid_confidence = True
                continue
            decision = candidate
            confidence = candidate_confidence
            break
        if confidence is None or not ZERO <= confidence <= Decimal("1"):
            skipped += 1
            if not closed.decision_ids:
                skipped_reasons["no_decision_id_on_fill"] += 1
            elif matched_decisions == 0:
                skipped_reasons[
                    "decision_not_in_history"
                    if history_checked
                    else "no_matching_cio_decision"
                ] += 1
            elif execute_decisions == 0:
                skipped_reasons["non_execute_decision"] += 1
            elif invalid_confidence and not missing_confidence:
                skipped_reasons["invalid_confidence"] += 1
            else:
                skipped_reasons["missing_confidence"] += 1
            continue
        selected_decision = cast(dict[str, Any], decision)
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
                "decision_id": str(selected_decision["decision_id"]),
                "symbol": closed.symbol,
                "closed_at": closed.closed_at.isoformat(),
            }
        )
    return {
        "records": records,
        "skipped": skipped,
        "skipped_reasons": dict(sorted(skipped_reasons.items())),
    }


def _read_historic_decisions(
    decision_ids: set[str], mysql_uri: str | None
) -> tuple[list[dict[str, Any]], bool]:
    """Read missing decision IDs from permanent history through a read-only engine."""
    if not decision_ids or not mysql_uri:
        return [], False

    engine = create_read_only_engine(mysql_uri, role="adhoc")
    try:
        table = Table(
            "cio_decisions",
            MetaData(),
            Column("decision_id", String(128)),
            Column("strategy_id", String(128)),
            Column("timestamp", DateTime),
            Column("action", String(20)),
            Column("confidence", Numeric(6, 5)),
        )
        historic: list[dict[str, Any]] = []
        ordered_ids = sorted(decision_ids)
        with engine.connect() as connection:
            for start in range(0, len(ordered_ids), MYSQL_DECISION_BATCH_SIZE):
                batch = ordered_ids[start : start + MYSQL_DECISION_BATCH_SIZE]
                query = select(table).where(table.c.decision_id.in_(batch))
                historic.extend(dict(row._mapping) for row in connection.execute(query))
        return historic, True
    finally:
        mark_engine_closing(engine)


async def get_calibration_records(
    mongodb: Any,
    *,
    since: datetime | None = None,
    strategy_id: str | None = None,
    source: str = "on_demand",
    mysql_uri: str | None = None,
) -> dict[str, Any]:
    """Read bounded audit data through the database gateway and build records."""
    filters = {
        "event_type": {"$in": sorted(FILL_EVENT_TYPES)},
        "strategy_id": strategy_id,
    }
    read_started = monotonic()
    execution_events = await mongodb.find_filtered(
        "execution_events", filters=filters, limit=MAX_ROWS, sort_order=-1
    )
    decisions = await mongodb.find_filtered(
        "cio_decisions",
        filters={"strategy_id": strategy_id, "action": "execute"},
        limit=MAX_ROWS,
        sort_order=-1,
    )
    if len(execution_events) >= MAX_ROWS:
        logger.warning("calibration execution_events read reached cap: %d", MAX_ROWS)
    if len(decisions) >= MAX_ROWS:
        logger.warning("calibration cio_decisions read reached cap: %d", MAX_ROWS)
    # Read newest first so the cap keeps the newest rows, then back to oldest first: fills with an equal
    # ``fill_time`` (a close and a reopen in the same ms) must reach the round book in ingestion order.
    execution_events = list(reversed(execution_events))
    decisions = list(reversed(decisions))
    event_decision_ids = {
        str(row["decision_id"])
        for row in execution_events
        if row.get("event_type") in FILL_EVENT_TYPES and row.get("decision_id")
    }
    mongo_decision_ids = {
        str(row["decision_id"]) for row in decisions if row.get("decision_id")
    }
    missing_decision_ids = event_decision_ids - mongo_decision_ids
    historic_decisions: list[dict[str, Any]] = []
    history_checked = False
    if missing_decision_ids and mysql_uri:
        try:
            historic_decisions, history_checked = await asyncio.to_thread(
                _read_historic_decisions, missing_decision_ids, mysql_uri
            )
        except Exception as exc:
            logger.warning("calibration historic cio_decisions read failed: %s", exc)
    decisions = historic_decisions + decisions
    record_report_stage(
        "calibration", "read+decode", monotonic() - read_started, source=source
    )
    compute_started = monotonic()
    report = await asyncio.to_thread(
        build_calibration_records,
        execution_events,
        decisions,
        since=since,
        history_checked=history_checked,
    )
    record_report_stage(
        "calibration", "compute", monotonic() - compute_started, source=source
    )
    return report


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
    mysql_uri: str | None = None,
) -> dict[str, Any]:
    """Read the calibration report and add its operational freshness signal."""
    report = await get_calibration_records(
        mongodb, since=since, strategy_id=strategy_id, mysql_uri=mysql_uri
    )
    return {**report, **calibration_freshness(report["records"], now=now)}
