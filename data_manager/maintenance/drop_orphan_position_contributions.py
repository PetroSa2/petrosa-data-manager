"""One-shot migration: drop the orphan `position_contributions` MySQL table.

Targets AC2 of `PetroSa2/petrosa-data-manager#221`. The full AC1 evidence
pack that justifies this drop is in `docs/audit-orphan-tables-2026-06-09.md`.
The TL;DR: the table's only Python writer (`strategy_position_manager.py`
in tradeengine) is dead code not imported by any deployed service, and
production tradeengine writes positions to the `positions` table via
`shared/mysql_client.py:create_position`. data-manager itself never reads
or writes the table.

Operator invocation:

    # Dry-run: print the SQL that would execute, verify the table is empty,
    # do not modify the database.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_orphan_position_contributions --dry-run

    # Apply: row-count guard runs first; aborts if rows > 0.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_orphan_position_contributions --apply

Exit codes:
    0  — success (drop applied, or dry-run completed cleanly)
    2  — MYSQL_URI not set
    3  — row-count guard tripped (table has rows; refuse to drop)
    4  — database error
    5  — invocation error (mutually-exclusive flags, etc.)

The script is intentionally **idempotent** (uses `DROP TABLE IF EXISTS`) so
re-running after a successful drop is a no-op.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import sqlalchemy as sa
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

TARGET_TABLE = "position_contributions"
TARGET_SCHEMA = "petrosa_crypto"
COUNT_SQL = f"SELECT COUNT(*) AS n FROM {TARGET_TABLE}"  # noqa: S608 — constant
DROP_SQL = f"DROP TABLE IF EXISTS {TARGET_TABLE}"  # noqa: S608 — constant
EXISTS_SQL = (
    "SELECT TABLE_NAME FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
)


class RowCountGuardError(RuntimeError):
    """Raised when the target table has rows; drop is refused."""


def table_exists(engine: Engine, *, schema: str = TARGET_SCHEMA) -> bool:
    """Return True if the target table exists in the given schema."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(EXISTS_SQL),
            {"schema": schema, "table": TARGET_TABLE},
        ).fetchone()
    return row is not None


def count_rows(engine: Engine) -> int:
    """Return the row count of the target table."""
    with engine.connect() as conn:
        row = conn.execute(sa.text(COUNT_SQL)).fetchone()
    if row is None:
        return 0
    return int(row[0])


def drop_table(engine: Engine) -> None:
    """Execute the idempotent DROP TABLE statement."""
    with engine.begin() as conn:
        conn.execute(sa.text(DROP_SQL))


def execute_migration(engine: Engine, *, dry_run: bool) -> dict[str, object]:
    """Run the migration end-to-end against the provided engine.

    Returns a result dict describing what was observed and done. Raises
    RowCountGuardError when the table has rows and dry_run is False.
    """
    result: dict[str, object] = {
        "target_schema": TARGET_SCHEMA,
        "target_table": TARGET_TABLE,
        "dry_run": dry_run,
        "table_existed": False,
        "row_count": 0,
        "dropped": False,
        "sql_planned": DROP_SQL,
    }

    if not table_exists(engine):
        logger.info(
            "drop_orphan_position_contributions: %s.%s not present — nothing to do",
            TARGET_SCHEMA,
            TARGET_TABLE,
        )
        return result

    result["table_existed"] = True
    rows = count_rows(engine)
    result["row_count"] = rows

    if rows > 0:
        if dry_run:
            logger.warning(
                "drop_orphan_position_contributions: dry-run sees %d rows in %s — "
                "drop would be refused; the table is NOT empty",
                rows,
                TARGET_TABLE,
            )
            return result
        raise RowCountGuardError(
            f"refusing to drop {TARGET_TABLE}: row-count guard tripped (rows={rows}); "
            "the audit's orphan classification is no longer valid"
        )

    if dry_run:
        logger.info(
            "drop_orphan_position_contributions: dry-run — would execute: %s",
            DROP_SQL,
        )
        return result

    drop_table(engine)
    result["dropped"] = True
    logger.info("drop_orphan_position_contributions: dropped %s", TARGET_TABLE)
    return result


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.drop_orphan_position_contributions",
        description=(
            "One-shot drop of the orphan `position_contributions` MySQL table. "
            "See docs/audit-orphan-tables-2026-06-09.md for the AC1 evidence."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned SQL + row count without modifying the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the drop (guarded by row-count check).",
    )
    return parser


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _make_engine_from_env() -> Engine:
    uri = os.getenv("MYSQL_URI")
    if not uri:
        raise RuntimeError("MYSQL_URI is not set; cannot connect to MySQL")
    return sa.create_engine(uri, future=True)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = _build_argparser()
    args = parser.parse_args(argv)

    try:
        engine = _make_engine_from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    try:
        execute_migration(engine, dry_run=args.dry_run)
    except RowCountGuardError as exc:
        logger.error("%s", exc)
        return 3
    except sa.exc.SQLAlchemyError as exc:
        logger.error("database error during migration: %s", exc)
        return 4
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(main())
