"""Confidence calibration records built from the immutable audit collections."""

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, cast

from sqlalchemy import Column, DateTime, MetaData, Numeric, String, Table, select

from data_manager.services.report_precompute import record_report_stage
from data_manager.services.round_book import (
    FILL_EVENT_TYPES,
    ClosedRound,
    RoundBook,
    _when,
)

MAX_ROWS = 50_000
MYSQL_DECISION_BATCH_SIZE = 1_000
#: The most decision ids looked up in MySQL history in one report; the ids of the newest closed rounds first.
MAX_HISTORIC_DECISION_IDS = 5_000
#: Actions of a decision that was executed. ``cio_decisions`` is fed only from the ``signals.trading.>`` subject
#: (the CIO publishes there only what it executes) and the side of the order is stored in ``action``
#: ("buy" / "sell"): production has no row with the action "execute" (petrosa-data-manager#585). "execute" is
#: kept for rows and callers that spell it that way. Anything else is a ``non_execute_decision``.
EXECUTED_DECISION_ACTIONS = frozenset({"execute", "buy", "sell"})
FRESHNESS_MAX_AGE_MINUTES = 30
ZERO = Decimal("0")
logger = logging.getLogger(__name__)


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


class CalibrationHistoryUnavailable(RuntimeError):
    """The MySQL decision history could not be read and the report would be missing decisions."""


def is_executed_decision(row: dict[str, Any]) -> bool:
    return str(row.get("action", "")).lower() in EXECUTED_DECISION_ACTIONS


def _executed_action_filter() -> dict[str, str]:
    actions = "|".join(sorted(EXECUTED_DECISION_ACTIONS))
    return {"$regex": f"^({actions})$", "$options": "i"}


def replay_closed_rounds(
    execution_events: list[dict[str, Any]], *, since: datetime | None = None
) -> list[ClosedRound]:
    """The closed rounds of the fills (closed at or after ``since`` when given), from the round book."""
    book = RoundBook()
    ordered = sorted(
        (row for row in execution_events if row.get("event_type") in FILL_EVENT_TYPES),
        key=lambda row: _when(row) or datetime.min.replace(tzinfo=UTC),
    )
    for row in ordered:
        book.apply(row)
    return [c for c in book.closed if since is None or c.closed_at >= since]


def build_calibration_records(
    execution_events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    since: datetime | None = None,
    history_checked: bool = False,
    history_lookup_capped_ids: set[str] | None = None,
    closed_rounds: list[ClosedRound] | None = None,
) -> dict[str, Any]:
    """Join closed round-book entries to executed CIO decisions."""
    closed_list = (
        closed_rounds
        if closed_rounds is not None
        else replay_closed_rounds(execution_events, since=since)
    )
    decisions_by_id = {
        str(row.get("decision_id")): row for row in decisions if row.get("decision_id")
    }
    records: list[dict[str, Any]] = []
    skipped = 0
    skipped_reasons: Counter[str] = Counter()
    for closed in closed_list:
        entry_decision_id = closed.decision_ids[0] if closed.decision_ids else None
        decision = decisions_by_id.get(entry_decision_id) if entry_decision_id else None
        confidence = (
            _decimal(decision.get("confidence"))
            if decision and is_executed_decision(decision)
            else None
        )
        if confidence is None or not ZERO <= confidence <= Decimal("1"):
            skipped += 1
            if entry_decision_id is None:
                skipped_reasons["entry_decision_unavailable"] += 1
            elif (
                decision is None
                and history_lookup_capped_ids
                and entry_decision_id in history_lookup_capped_ids
            ):
                skipped_reasons["history_lookup_capped"] += 1
            elif decision is None and history_checked:
                skipped_reasons["decision_not_in_history"] += 1
            elif decision is None:
                skipped_reasons["no_matching_cio_decision"] += 1
            elif not is_executed_decision(decision):
                skipped_reasons["non_execute_decision"] += 1
            else:
                candidate_confidence = _decimal(decision.get("confidence"))
                if (
                    candidate_confidence is not None
                    and not ZERO <= candidate_confidence <= Decimal("1")
                ):
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


_DECISIONS_TABLE = Table(
    "cio_decisions",
    MetaData(),
    Column("decision_id", String(128)),
    Column("strategy_id", String(128)),
    Column("timestamp", DateTime),
    Column("action", String(20)),
    Column("confidence", Numeric(6, 5)),
)


def _read_historic_decisions(
    engine: Any, decision_ids: set[str]
) -> list[dict[str, Any]]:
    """The permanent MySQL copy of ``cio_decisions`` for some ids, in batches, through an existing engine.

    The engine is the serving adapter's: its pool, connect and read timeouts, and no connection of its own
    against ``max_user_connections``. Read-only; runs in a worker thread.
    """
    historic: list[dict[str, Any]] = []
    ordered_ids = sorted(decision_ids)
    with engine.connect() as connection:
        for start in range(0, len(ordered_ids), MYSQL_DECISION_BATCH_SIZE):
            batch = ordered_ids[start : start + MYSQL_DECISION_BATCH_SIZE]
            query = select(_DECISIONS_TABLE).where(
                _DECISIONS_TABLE.c.decision_id.in_(batch)
            )
            historic.extend(dict(row._mapping) for row in connection.execute(query))
    return historic


def _bounded_missing_ids(
    closed_rounds: list[ClosedRound], known_ids: set[str]
) -> set[str]:
    """The entry decision ids Mongo does not have, newest rounds first, at most the cap."""
    wanted: dict[str, None] = {}  # insertion-ordered: the newest rounds' ids come first
    for closed in sorted(closed_rounds, key=lambda c: c.closed_at, reverse=True):
        if closed.decision_ids and closed.decision_ids[0] not in known_ids:
            wanted.setdefault(closed.decision_ids[0])
    if len(wanted) > MAX_HISTORIC_DECISION_IDS:
        logger.warning(
            "calibration historic decision lookup reached cap: %d",
            MAX_HISTORIC_DECISION_IDS,
        )
        return set(list(wanted)[:MAX_HISTORIC_DECISION_IDS])
    return set(wanted)


async def get_calibration_records(
    mongodb: Any,
    *,
    since: datetime | None = None,
    strategy_id: str | None = None,
    source: str = "on_demand",
    mysql_adapter: Any | None = None,
    strict_history: bool = False,
) -> dict[str, Any]:
    """Read bounded audit data through the database gateway and build records.

    Mongo ``cio_decisions`` keeps one day (TTL); the permanent copy is in MySQL. The decisions of closed rounds
    that Mongo no longer has are looked up there, through the serving adapter's engine. When that lookup is
    impossible the report is missing decisions: with ``strict_history`` (a report that is cached) it raises
    ``CalibrationHistoryUnavailable``, so a last good cache row is kept and nothing is cached; otherwise the
    report is returned with ``history_unavailable: true``.
    """
    filters = {
        "event_type": {"$in": sorted(FILL_EVENT_TYPES)},
        "strategy_id": strategy_id,
    }
    read_started = monotonic()
    execution_events = await mongodb.find_filtered(
        "execution_events",
        filters=filters,
        limit=MAX_ROWS,
        sort_order=-1,
    )
    decisions = await mongodb.find_filtered(
        "cio_decisions",
        filters={"strategy_id": strategy_id, "action": _executed_action_filter()},
        limit=MAX_ROWS,
        sort_order=-1,
    )
    if len(execution_events) >= MAX_ROWS:
        logger.warning("calibration execution_events read reached cap: %d", MAX_ROWS)
    if len(decisions) >= MAX_ROWS:
        logger.warning("calibration cio_decisions read reached cap: %d", MAX_ROWS)
    # Keep this single-key sort index-backed. Mongo uses insertion order for timestamp ties;
    # these collection IDs are not insertion-ordered and must not be used as a tiebreaker.
    # Read newest first so the cap keeps the newest rows, then back to oldest first: fills with an equal
    # ``fill_time`` (a close and a reopen in the same ms) must reach the round book in ingestion order.
    execution_events = list(reversed(execution_events))
    decisions = list(reversed(decisions))
    closed_rounds = await asyncio.to_thread(
        replay_closed_rounds, execution_events, since=since
    )
    mongo_decision_ids = {
        str(row["decision_id"]) for row in decisions if row.get("decision_id")
    }
    all_missing_entry_ids = {
        closed.decision_ids[0]
        for closed in closed_rounds
        if closed.decision_ids and closed.decision_ids[0] not in mongo_decision_ids
    }
    wanted = _bounded_missing_ids(closed_rounds, mongo_decision_ids)
    history_lookup_capped_ids = all_missing_entry_ids - wanted
    historic_decisions: list[dict[str, Any]] = []
    history_checked = True
    history_unavailable = False
    if wanted:
        engine = getattr(mysql_adapter, "engine", None)
        try:
            if engine is None:
                raise CalibrationHistoryUnavailable("no MySQL engine")
            historic_decisions = await asyncio.to_thread(
                _read_historic_decisions, engine, wanted
            )
        except Exception as exc:
            logger.warning("calibration historic cio_decisions read failed: %s", exc)
            if strict_history:
                raise CalibrationHistoryUnavailable(str(exc)) from exc
            history_checked = False
            history_unavailable = True
    # Mongo, the live copy, wins over the MySQL copy of the same decision (it is later in the list).
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
        history_lookup_capped_ids=history_lookup_capped_ids,
        closed_rounds=closed_rounds,
    )
    if history_unavailable:
        report["history_unavailable"] = True
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
    mysql_adapter: Any | None = None,
    strict_history: bool = False,
) -> dict[str, Any]:
    """Read the calibration report and add its operational freshness signal."""
    report = await get_calibration_records(
        mongodb,
        since=since,
        strategy_id=strategy_id,
        mysql_adapter=mysql_adapter,
        strict_history=strict_history,
    )
    return {**report, **calibration_freshness(report["records"], now=now)}
