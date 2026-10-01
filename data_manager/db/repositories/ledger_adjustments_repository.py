"""MySQL repository for insert-only ledger adjustments and atomic supersede writes."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from data_manager.db.repositories.base_repository import BaseRepository

MONEY_COLUMNS = frozenset(
    {
        "pnl",
        "pnl_pct",
        "pnl_after_fees",
        "commission_total",
        "final_commission",
        "entry_price",
        "exit_price",
        "quantity",
    }
)
POSITION_MUTABLE_COLUMNS = frozenset({"status", "close_reason", "pnl_unknown"})


class AdjustmentValidationError(ValueError):
    """Raised when an adjustment payload violates policy constraints."""


class AdjustmentConflictError(RuntimeError):
    """Raised when an adjustment cannot be applied due to row drift."""


class LedgerAdjustmentsRepository(BaseRepository):
    """Insert-only audit chain repository."""

    _chain_lock = threading.Lock()
    _position_locks: dict[int, threading.Lock] = {}
    _position_locks_guard = threading.Lock()

    @staticmethod
    def canonical_json(value: Any) -> str:
        """Serialize data with sorted keys and no whitespace."""
        return json.dumps(
            LedgerAdjustmentsRepository._normalize_for_canonical_json(value),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _normalize_for_canonical_json(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): LedgerAdjustmentsRepository._normalize_for_canonical_json(
                    item
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                LedgerAdjustmentsRepository._normalize_for_canonical_json(item)
                for item in value
            ]
        if isinstance(value, datetime):
            instant = (
                value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            )
            return instant.isoformat().replace("+00:00", "Z")
        if isinstance(value, Decimal):
            return format(value, "f")
        return value

    @staticmethod
    def _compute_row_hash(prev_hash: str | None, row_payload: dict[str, Any]) -> str:
        material = f"{prev_hash or ''}{LedgerAdjustmentsRepository.canonical_json(row_payload)}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _changed_keys(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
        keys = set(before) | set(after)
        return {key for key in keys if before.get(key) != after.get(key)}

    @staticmethod
    def _validate_position_adjustment(
        before: dict[str, Any], after: dict[str, Any], *, enforce_superseded: bool
    ) -> None:
        changed = LedgerAdjustmentsRepository._changed_keys(before, after)
        if any(before.get(key) != after.get(key) for key in MONEY_COLUMNS):
            raise AdjustmentValidationError("monetary columns cannot be changed")
        illegal = changed - POSITION_MUTABLE_COLUMNS
        if illegal:
            raise AdjustmentValidationError(
                "only status, close_reason, pnl_unknown may differ"
            )
        if enforce_superseded:
            if after.get("status") != "superseded":
                raise AdjustmentValidationError("status must be superseded")
            if after.get("pnl_unknown") is not True:
                raise AdjustmentValidationError("pnl_unknown must be true")

    @staticmethod
    def _ensure_decimal_strings_for_monetary(data: dict[str, Any]) -> None:
        for column in MONEY_COLUMNS:
            if column not in data or data[column] is None:
                continue
            value = data[column]
            if not isinstance(value, str):
                raise AdjustmentValidationError(
                    f"{column} must be a decimal string in audit payloads"
                )
            try:
                Decimal(value)
            except Exception as exc:  # pragma: no cover - Decimal defines exceptions
                raise AdjustmentValidationError(
                    f"{column} must be a decimal string in audit payloads"
                ) from exc

    @staticmethod
    def verify_hash_chain(rows: list[dict[str, Any]]) -> bool:
        prev_hash: str | None = None
        for row in rows:
            payload = {
                "adjustment_id": row["adjustment_id"],
                "applied_at": row["applied_at"],
                "applied_by": row["applied_by"],
                "approved_by": row["approved_by"],
                "target_table": row["target_table"],
                "target_key": row["target_key"],
                "before": row["before"],
                "after": row["after"],
                "reason_code": row["reason_code"],
                "evidence_ref": row["evidence_ref"],
                "run_mode": row["run_mode"],
                "dry_run_adjustment_id": row.get("dry_run_adjustment_id"),
                "prev_hash": row.get("prev_hash"),
            }
            expected = LedgerAdjustmentsRepository._compute_row_hash(
                row.get("prev_hash"), payload
            )
            if row.get("row_hash") != expected:
                return False
            if row.get("prev_hash") != prev_hash:
                return False
            prev_hash = row["row_hash"]
        return True

    def _engine(self):
        if self.mysql is None:
            raise RuntimeError("MySQL is not available")
        return self.mysql._ensure_connected()

    @classmethod
    def _position_lock(cls, position_id: int) -> threading.Lock:
        with cls._position_locks_guard:
            lock = cls._position_locks.get(position_id)
            if lock is None:
                lock = threading.Lock()
                cls._position_locks[position_id] = lock
            return lock

    @staticmethod
    def _as_utc_string(value: Any) -> str:
        if isinstance(value, datetime):
            instant = (
                value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            )
            return instant.isoformat().replace("+00:00", "Z")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _validate_apply_reference(
        self,
        connection: Any,
        payload: dict[str, Any],
        before_json: str,
        after_json: str,
    ) -> None:
        if payload["run_mode"] != "apply":
            return
        ref_id = payload.get("dry_run_adjustment_id")
        if not ref_id:
            raise AdjustmentValidationError(
                "dry_run_adjustment_id is required for apply"
            )
        row = (
            connection.execute(
                text(
                    "SELECT target_table, target_key, `before`, `after`, run_mode "
                    "FROM ledger_adjustments WHERE adjustment_id = :id"
                ),
                {"id": ref_id},
            )
            .mappings()
            .first()
        )
        if not row or row["run_mode"] != "dry_run":
            raise AdjustmentValidationError(
                "apply requires an earlier matching dry_run adjustment"
            )
        if (
            row["target_table"] != payload["target_table"]
            or row["target_key"] != payload["target_key"]
            or self.canonical_json(
                json.loads(row["before"])
                if isinstance(row["before"], str)
                else row["before"]
            )
            != self.canonical_json(json.loads(before_json))
            or self.canonical_json(
                json.loads(row["after"])
                if isinstance(row["after"], str)
                else row["after"]
            )
            != self.canonical_json(json.loads(after_json))
        ):
            raise AdjustmentValidationError(
                "apply requires an earlier matching dry_run adjustment"
            )

    @staticmethod
    def _select_for_update(connection: Any, base_query: str, params: dict[str, Any]):
        dialect = connection.dialect.name
        statement = f"{base_query} FOR UPDATE" if dialect != "sqlite" else base_query
        return connection.execute(text(statement), params)

    def insert_adjustment(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._chain_lock:
            engine = self._engine()
            with engine.begin() as connection:
                return self._insert_adjustment_in_transaction(connection, payload)

    def _insert_adjustment_in_transaction(
        self, connection: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        before = dict(payload["before"])
        after = dict(payload["after"])
        self._ensure_decimal_strings_for_monetary(before)
        self._ensure_decimal_strings_for_monetary(after)
        if payload["target_table"] == "positions":
            self._validate_position_adjustment(
                before,
                after,
                enforce_superseded=payload["reason_code"] == "phantom_superseded"
                or payload["run_mode"] == "apply",
            )

        before_json = self.canonical_json(before)
        after_json = self.canonical_json(after)
        applied_at = self._as_utc_string(payload["applied_at"])
        latest = (
            self._select_for_update(
                connection,
                "SELECT row_hash FROM ledger_adjustments "
                "ORDER BY chain_position DESC LIMIT 1",
                {},
            )
            .mappings()
            .first()
        )
        prev_hash = latest["row_hash"] if latest else None
        self._validate_apply_reference(connection, payload, before_json, after_json)
        row_payload = {
            "adjustment_id": payload["adjustment_id"],
            "applied_at": applied_at,
            "applied_by": payload["applied_by"],
            "approved_by": payload["approved_by"],
            "target_table": payload["target_table"],
            "target_key": payload["target_key"],
            "before": json.loads(before_json),
            "after": json.loads(after_json),
            "reason_code": payload["reason_code"],
            "evidence_ref": payload["evidence_ref"],
            "run_mode": payload["run_mode"],
            "dry_run_adjustment_id": payload.get("dry_run_adjustment_id"),
            "prev_hash": prev_hash,
        }
        row_hash = self._compute_row_hash(prev_hash, row_payload)
        connection.execute(
            text(
                "INSERT INTO ledger_adjustments ("
                "adjustment_id, applied_at, applied_by, approved_by, "
                "target_table, target_key, `before`, `after`, reason_code, "
                "evidence_ref, run_mode, dry_run_adjustment_id, prev_hash, row_hash"
                ") VALUES ("
                ":adjustment_id, :applied_at, :applied_by, :approved_by, "
                ":target_table, :target_key, :before, :after, :reason_code, "
                ":evidence_ref, :run_mode, :dry_run_adjustment_id, :prev_hash, :row_hash"
                ")"
            ),
            {
                **row_payload,
                "before": before_json,
                "after": after_json,
                "row_hash": row_hash,
            },
        )
        return {
            "adjustment_id": row_payload["adjustment_id"],
            "target_table": row_payload["target_table"],
            "target_key": row_payload["target_key"],
            "run_mode": row_payload["run_mode"],
            "prev_hash": prev_hash,
            "row_hash": row_hash,
        }

    def list_adjustments(
        self,
        *,
        target_table: str | None = None,
        target_key: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        engine = self._engine()
        with engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT adjustment_id, applied_at, applied_by, approved_by, "
                        "target_table, target_key, `before`, `after`, reason_code, "
                        "evidence_ref, run_mode, dry_run_adjustment_id, prev_hash, row_hash, "
                        "chain_position "
                        "FROM ledger_adjustments ORDER BY chain_position"
                    )
                )
                .mappings()
                .all()
            )
        result: list[dict[str, Any]] = []
        for row in rows:
            materialized = {
                **dict(row),
                "before": json.loads(row["before"])
                if isinstance(row["before"], str)
                else row["before"],
                "after": json.loads(row["after"])
                if isinstance(row["after"], str)
                else row["after"],
            }
            if target_table and materialized["target_table"] != target_table:
                continue
            if target_key and materialized["target_key"] != target_key:
                continue
            applied_at = (
                materialized["applied_at"]
                if isinstance(materialized["applied_at"], datetime)
                else datetime.fromisoformat(
                    str(materialized["applied_at"]).replace("Z", "+00:00")
                )
            )
            if from_time and applied_at < from_time:
                continue
            if to_time and applied_at > to_time:
                continue
            result.append(materialized)
        return result

    def supersede_position(self, payload: dict[str, Any]) -> dict[str, Any]:
        position_id = int(payload["id"])
        expected_before = dict(payload["expected_before"])
        self._ensure_decimal_strings_for_monetary(expected_before)
        lock = self._position_lock(position_id)
        with lock, self._chain_lock:
            engine = self._engine()
            with engine.begin() as connection:
                row = (
                    self._select_for_update(
                        connection,
                        "SELECT * FROM positions WHERE id = :id",
                        {"id": position_id},
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    raise LookupError("position not found")
                current = {
                    key: (format(value, "f") if isinstance(value, Decimal) else value)
                    for key, value in dict(row).items()
                }
                for key, value in expected_before.items():
                    if current.get(key) != value:
                        raise AdjustmentConflictError("position changed since dry_run")
                before = {
                    "status": current.get("status"),
                    "close_reason": current.get("close_reason"),
                    "pnl_unknown": bool(current.get("pnl_unknown")),
                    **{
                        key: current.get(key)
                        for key in MONEY_COLUMNS
                        if key in current and current.get(key) is not None
                    },
                }
                after = {
                    **before,
                    "status": "superseded",
                    "close_reason": payload["reason_code"],
                    "pnl_unknown": True,
                }
                self._validate_position_adjustment(
                    before, after, enforce_superseded=True
                )
                adjustment = self._insert_adjustment_in_transaction(
                    connection,
                    {
                        "adjustment_id": payload["adjustment_id"],
                        "applied_at": payload["applied_at"],
                        "applied_by": payload["applied_by"],
                        "approved_by": payload["approved_by"],
                        "target_table": "positions",
                        "target_key": str(position_id),
                        "before": before,
                        "after": after,
                        "reason_code": payload["reason_code"],
                        "evidence_ref": payload["evidence_ref"],
                        "run_mode": "apply",
                        "dry_run_adjustment_id": payload["dry_run_adjustment_id"],
                    },
                )
                updated = connection.execute(
                    text(
                        "UPDATE positions SET status = :status, close_reason = :close_reason, "
                        "pnl_unknown = :pnl_unknown WHERE id = :id"
                    ),
                    {
                        "id": position_id,
                        "status": "superseded",
                        "close_reason": payload["reason_code"],
                        "pnl_unknown": 1,
                    },
                )
                if int(updated.rowcount or 0) != 1:
                    raise RuntimeError("position update failed")
                return {
                    "id": position_id,
                    "status": "superseded",
                    "adjustment_id": adjustment["adjustment_id"],
                }
