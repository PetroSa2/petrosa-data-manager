"""Safely repair the exhausted ``positions`` BIGINT AUTO_INCREMENT counter.

The default invocation is read-only. Applying the repair requires explicit
confirmation of the table and of W1 deployment because the repair assumes that
new position ids will no longer be populated from UUID strings.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

TARGET_TABLE = "positions"
ID_THRESHOLD = 10**12
MAX_ID_SQL = "SELECT MAX(id) AS max_id FROM positions"
MAX_REGULAR_ID_SQL = "SELECT MAX(id) AS max_id FROM positions WHERE id <= :threshold"
COERCED_ROWS_SQL = "SELECT * FROM positions WHERE id > :threshold ORDER BY id"
AUTO_INCREMENT_SQL = """
SELECT AUTO_INCREMENT AS auto_increment
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name
"""
REFERENCING_FK_SQL = """
SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_NAME = :table_name
  AND REFERENCED_COLUMN_NAME = 'id'
"""
UPDATE_ID_SQL = "UPDATE positions SET id = :new_id WHERE id = :old_id"


class RepairSafetyError(RuntimeError):
    """Raised when the repair cannot safely proceed."""


@dataclass(frozen=True)
class RepairPlan:
    max_id: int | None
    max_regular_id: int
    auto_increment: int | None
    coerced_rows: tuple[dict[str, Any], ...]
    updates: tuple[dict[str, int], ...]
    final_auto_increment: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": TARGET_TABLE,
            "threshold": ID_THRESHOLD,
            "max_id": self.max_id,
            "max_regular_id": self.max_regular_id,
            "auto_increment_before": self.auto_increment,
            "coerced_row_count": len(self.coerced_rows),
            "updates": list(self.updates),
            "final_auto_increment": self.final_auto_increment,
        }


def _row_value(row: Any, name: str, index: int = 0) -> Any:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and name in mapping:
        return mapping[name]
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return row[index]


def _read_scalar(
    engine: Engine, statement: str, parameters: dict[str, Any] | None = None
) -> Any:
    with engine.connect() as connection:
        row = connection.execute(sa.text(statement), parameters or {}).fetchone()
    return None if row is None else _row_value(row, "max_id")


def read_max_id(engine: Engine) -> int | None:
    value = _read_scalar(engine, MAX_ID_SQL)
    return None if value is None else int(value)


def read_max_regular_id(engine: Engine) -> int:
    value = _read_scalar(engine, MAX_REGULAR_ID_SQL, {"threshold": ID_THRESHOLD})
    return 0 if value is None else int(value)


def read_auto_increment(engine: Engine) -> int | None:
    if engine.dialect.name != "mysql":
        return None
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(AUTO_INCREMENT_SQL), {"table_name": TARGET_TABLE}
        ).fetchone()
    value = None if row is None else _row_value(row, "auto_increment")
    return None if value is None else int(value)


def read_coerced_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(COERCED_ROWS_SQL), {"threshold": ID_THRESHOLD}
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def foreign_key_references(engine: Engine) -> list[dict[str, Any]]:
    if engine.dialect.name == "mysql":
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(REFERENCING_FK_SQL), {"table_name": TARGET_TABLE}
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    inspector = sa.inspect(engine)
    references: list[dict[str, Any]] = []
    for table_name in inspector.get_table_names():
        for foreign_key in inspector.get_foreign_keys(table_name):
            referred_columns = foreign_key.get("referred_columns") or []
            if (
                foreign_key.get("referred_table") == TARGET_TABLE
                and "id" in referred_columns
            ):
                references.append(
                    {
                        "table_name": table_name,
                        "constraint_name": foreign_key.get("name"),
                        "column_name": (
                            foreign_key.get("constrained_columns") or [None]
                        )[0],
                    }
                )
    return references


def build_plan(engine: Engine) -> RepairPlan:
    rows = sorted(read_coerced_rows(engine), key=lambda row: int(row["id"]))
    max_regular_id = read_max_regular_id(engine)
    updates = tuple(
        {"old_id": int(row["id"]), "new_id": max_regular_id + offset}
        for offset, row in enumerate(rows, start=1)
    )
    new_max_id = max_regular_id + len(updates)
    return RepairPlan(
        max_id=read_max_id(engine),
        max_regular_id=max_regular_id,
        auto_increment=read_auto_increment(engine),
        coerced_rows=tuple(rows),
        updates=updates,
        final_auto_increment=new_max_id + 1,
    )


def set_auto_increment(engine: Engine, value: int) -> None:
    if engine.dialect.name != "mysql":
        return
    with engine.begin() as connection:
        connection.execute(
            sa.text(f"ALTER TABLE {TARGET_TABLE} AUTO_INCREMENT = {value}")
        )


def apply_plan(engine: Engine, plan: RepairPlan) -> int | None:
    references = foreign_key_references(engine)
    if references:
        raise RepairSafetyError(
            "refusing repair: foreign keys reference positions.id: "
            + json.dumps(references, sort_keys=True)
        )
    for update in plan.updates:
        with engine.begin() as connection:
            connection.execute(sa.text(UPDATE_ID_SQL), update)
    set_auto_increment(engine, plan.final_auto_increment)
    return read_auto_increment(engine)


def execute_repair(engine: Engine, *, dry_run: bool) -> dict[str, Any]:
    plan = build_plan(engine)
    result = plan.as_dict()
    result["dry_run"] = dry_run
    result["applied"] = False
    if dry_run:
        return result
    applied_auto_increment = apply_plan(engine, plan)
    result["applied"] = True
    result["auto_increment_after"] = applied_auto_increment
    return result


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.repair_positions_auto_increment",
        description="Plan or apply a guarded repair of positions AUTO_INCREMENT.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="Print the plan without writes."
    )
    mode.add_argument("--apply", action="store_true", help="Apply the guarded repair.")
    parser.add_argument(
        "--confirm-table",
        choices=(TARGET_TABLE,),
        help="Required with --apply to confirm the target table.",
    )
    parser.add_argument(
        "--i-confirm-w1-deployed",
        action="store_true",
        help="Confirm that W1 is deployed and UUID-to-integer injection is fixed.",
    )
    return parser


def _configure_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )


def _make_engine_from_env() -> Engine:
    uri = os.getenv("MYSQL_URI")
    if not uri:
        raise RuntimeError("MYSQL_URI is not set; cannot connect to MySQL")
    return sa.create_engine(uri, future=True)


def _w1_confirmed(args: argparse.Namespace) -> bool:
    return args.i_confirm_w1_deployed or os.getenv("W1_DEPLOYED", "").lower() in {
        "1",
        "true",
        "yes",
    }


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = _build_argparser()
    args = parser.parse_args(argv)
    dry_run = not args.apply

    if not args.apply and (args.confirm_table or args.i_confirm_w1_deployed):
        parser.error("confirmation flags require --apply")
    if args.apply and args.confirm_table != TARGET_TABLE:
        logger.error("--apply requires --confirm-table positions")
        return 5
    if args.apply and not _w1_confirmed(args):
        logger.error("--apply requires --i-confirm-w1-deployed or W1_DEPLOYED=true")
        return 5

    try:
        engine = _make_engine_from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    try:
        result = execute_repair(engine, dry_run=dry_run)
        print(json.dumps(result, sort_keys=True))
        return 0
    except RepairSafetyError as exc:
        logger.error("%s", exc)
        return 3
    except SQLAlchemyError as exc:
        logger.error("database error during repair: %s", exc)
        return 4
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
