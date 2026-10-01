from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from data_manager.db.repositories.ledger_adjustments_repository import (
    AdjustmentConflictError,
    AdjustmentValidationError,
    LedgerAdjustmentsRepository,
)


class SqliteAdapter:
    def __init__(self, engine):
        self.engine = engine

    def _ensure_connected(self):
        return self.engine


def _engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _create_schema(engine, *, include_close_reason: bool = True):
    close_reason = "close_reason TEXT," if include_close_reason else ""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE positions ("
                "id INTEGER PRIMARY KEY,"
                "position_id TEXT,"
                "status TEXT,"
                f"{close_reason}"
                "pnl_unknown INTEGER DEFAULT 0,"
                "pnl TEXT,"
                "pnl_pct TEXT,"
                "pnl_after_fees TEXT,"
                "commission_total TEXT,"
                "final_commission TEXT,"
                "entry_price TEXT,"
                "exit_price TEXT,"
                "quantity TEXT"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE ledger_adjustments ("
                "chain_position INTEGER PRIMARY KEY AUTOINCREMENT,"
                "adjustment_id TEXT NOT NULL UNIQUE,"
                "applied_at TEXT NOT NULL,"
                "applied_by TEXT NOT NULL,"
                "approved_by TEXT NOT NULL,"
                "target_table TEXT NOT NULL,"
                "target_key TEXT NOT NULL,"
                "`before` TEXT NOT NULL,"
                "`after` TEXT NOT NULL,"
                "reason_code TEXT NOT NULL,"
                "evidence_ref TEXT NOT NULL,"
                "run_mode TEXT NOT NULL,"
                "dry_run_adjustment_id TEXT,"
                "prev_hash TEXT,"
                "row_hash TEXT NOT NULL"
                ")"
            )
        )


def _repo(engine):
    return LedgerAdjustmentsRepository(SqliteAdapter(engine), None)


def _adjustment(*, run_mode: str, dry_run_adjustment_id: str | None = None):
    return {
        "adjustment_id": str(uuid4()),
        "applied_at": datetime.now(UTC),
        "applied_by": "agent",
        "approved_by": "reviewer",
        "target_table": "positions",
        "target_key": "1",
        "before": {"status": "open", "close_reason": None, "pnl_unknown": False},
        "after": {
            "status": "superseded",
            "close_reason": "phantom_superseded",
            "pnl_unknown": True,
        },
        "reason_code": "phantom_superseded",
        "evidence_ref": "issue-467",
        "run_mode": run_mode,
        "dry_run_adjustment_id": dry_run_adjustment_id,
    }


def _seed_position(engine, *, status: str = "open", position_id: str = ""):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO positions (id, position_id, status, close_reason, pnl_unknown, pnl) "
                "VALUES (1, :position_id, :status, NULL, 0, NULL)"
            ),
            {"position_id": position_id, "status": status},
        )


def test_insert_apply_requires_matching_dry_run():
    engine = _engine()
    _create_schema(engine)
    repo = _repo(engine)

    dry_run = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(dry_run)

    apply_payload = _adjustment(
        run_mode="apply", dry_run_adjustment_id=dry_run["adjustment_id"]
    )
    apply_payload["before"] = dict(dry_run["before"])
    apply_payload["after"] = dict(dry_run["after"])
    apply_payload["target_key"] = dry_run["target_key"]
    repo.insert_adjustment(apply_payload)

    missing_ref = _adjustment(run_mode="apply", dry_run_adjustment_id=str(uuid4()))
    with pytest.raises(AdjustmentValidationError):
        repo.insert_adjustment(missing_ref)


def test_monetary_column_change_is_rejected():
    engine = _engine()
    _create_schema(engine)
    repo = _repo(engine)
    payload = _adjustment(run_mode="dry_run")
    payload["before"]["pnl"] = "0.00"
    payload["after"]["pnl"] = "1.00"
    with pytest.raises(AdjustmentValidationError):
        repo.insert_adjustment(payload)


def test_hash_chain_verification_detects_tampering():
    engine = _engine()
    _create_schema(engine)
    repo = _repo(engine)
    first = _adjustment(run_mode="dry_run")
    second = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(first)
    repo.insert_adjustment(second)
    rows = repo.list_adjustments()
    assert repo.verify_hash_chain(rows)
    rows[1]["after"]["close_reason"] = "tampered"
    assert repo.verify_hash_chain(rows) is False


def test_supersede_updates_by_primary_key_with_empty_position_id():
    engine = _engine()
    _create_schema(engine)
    _seed_position(engine, position_id="")
    repo = _repo(engine)
    dry_run = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(dry_run)

    result = repo.supersede_position(
        {
            "id": 1,
            "expected_before": {
                "status": "open",
                "pnl_unknown": 0,
                "close_reason": None,
            },
            "reason_code": "phantom_superseded",
            "evidence_ref": "issue-467",
            "applied_by": "agent",
            "approved_by": "reviewer",
            "dry_run_adjustment_id": dry_run["adjustment_id"],
            "adjustment_id": str(uuid4()),
            "applied_at": datetime.now(UTC),
        }
    )
    assert result["status"] == "superseded"
    with engine.begin() as connection:
        status = (
            connection.execute(
                text("SELECT status, position_id, pnl FROM positions WHERE id = 1")
            )
            .mappings()
            .first()
        )
    assert status["status"] == "superseded"
    assert status["position_id"] == ""
    assert status["pnl"] is None


def test_supersede_conflict_on_expected_before_drift():
    engine = _engine()
    _create_schema(engine)
    _seed_position(engine, status="superseded")
    repo = _repo(engine)
    dry_run = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(dry_run)
    with pytest.raises(AdjustmentConflictError):
        repo.supersede_position(
            {
                "id": 1,
                "expected_before": {"status": "open"},
                "reason_code": "phantom_superseded",
                "evidence_ref": "issue-467",
                "applied_by": "agent",
                "approved_by": "reviewer",
                "dry_run_adjustment_id": dry_run["adjustment_id"],
                "adjustment_id": str(uuid4()),
                "applied_at": datetime.now(UTC),
            }
        )


def test_supersede_rolls_back_when_audit_insert_fails():
    engine = _engine()
    _create_schema(engine)
    _seed_position(engine)
    repo = _repo(engine)
    dry_run = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(dry_run)

    payload = {
        "id": 1,
        "expected_before": {"status": "open", "close_reason": None, "pnl_unknown": 0},
        "reason_code": "phantom_superseded",
        "evidence_ref": "issue-467",
        "applied_by": "agent",
        "approved_by": "reviewer",
        "dry_run_adjustment_id": "missing-dry-run",
        "adjustment_id": str(uuid4()),
        "applied_at": datetime.now(UTC),
    }
    with pytest.raises(AdjustmentValidationError):
        repo.supersede_position(payload)

    with engine.begin() as connection:
        row = (
            connection.execute(text("SELECT status FROM positions WHERE id = 1"))
            .mappings()
            .first()
        )
    assert row["status"] == "open"


def test_supersede_rolls_back_when_position_update_fails():
    engine = _engine()
    _create_schema(engine, include_close_reason=False)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO positions (id, position_id, status, pnl_unknown, pnl) "
                "VALUES (1, '', 'open', 0, NULL)"
            )
        )
    repo = _repo(engine)
    dry_run = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(dry_run)

    with pytest.raises(Exception):
        repo.supersede_position(
            {
                "id": 1,
                "expected_before": {"status": "open", "pnl_unknown": 0},
                "reason_code": "phantom_superseded",
                "evidence_ref": "issue-467",
                "applied_by": "agent",
                "approved_by": "reviewer",
                "dry_run_adjustment_id": dry_run["adjustment_id"],
                "adjustment_id": str(uuid4()),
                "applied_at": datetime.now(UTC),
            }
        )
    with engine.begin() as connection:
        total = connection.execute(
            text("SELECT COUNT(*) FROM ledger_adjustments WHERE run_mode='apply'")
        ).scalar_one()
        status = connection.execute(
            text("SELECT status FROM positions WHERE id = 1")
        ).scalar_one()
    assert total == 0
    assert status == "open"


def test_concurrent_adjustments_and_supersede_are_linear_and_atomic():
    engine = _engine()
    _create_schema(engine)
    _seed_position(engine)
    repo = _repo(engine)
    dry_run = _adjustment(run_mode="dry_run")
    repo.insert_adjustment(dry_run)

    insert_payloads = [_adjustment(run_mode="dry_run"), _adjustment(run_mode="dry_run")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for payload in insert_payloads:
            pool.submit(repo.insert_adjustment, payload)

    rows = repo.list_adjustments()
    assert len(rows) == 3
    assert repo.verify_hash_chain(rows)

    payload = {
        "id": 1,
        "expected_before": {"status": "open", "close_reason": None, "pnl_unknown": 0},
        "reason_code": "phantom_superseded",
        "evidence_ref": "issue-467",
        "applied_by": "agent",
        "approved_by": "reviewer",
        "dry_run_adjustment_id": dry_run["adjustment_id"],
        "applied_at": datetime.now(UTC),
    }

    def _apply_once():
        try:
            repo.supersede_position({**payload, "adjustment_id": str(uuid4())})
            return "ok"
        except AdjustmentConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _apply_once(), range(2)))
    assert sorted(results) == ["conflict", "ok"]
    with engine.begin() as connection:
        applies = connection.execute(
            text("SELECT COUNT(*) FROM ledger_adjustments WHERE run_mode='apply'")
        ).scalar_one()
    assert applies == 1
