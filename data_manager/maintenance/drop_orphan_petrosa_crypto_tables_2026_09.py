"""Guarded drop migration for the 2026-09-02 `petrosa_crypto` dead-table audit.

Targets the retained legacy table from `PetroSa2/petrosa-data-manager#272` (follow-up to #221 / PR
#225's `drop_orphan_position_contributions.py`, which this module mirrors).

The persistence registry is the source of truth for durable tables. This
script retains only ``extraction_metadata`` and must never include a
registry-durable table; the registry test enforces that invariant before CI
can merge a change.

``datasets`` and ``lineage_records`` are explicitly NOT in ``TARGET_TABLES``
(AC5 — retained latent-feature tables; not orphan schema).

Operator invocation:

    # Dry-run: for every target table, print planned SQL + row count. No DDL.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_orphan_petrosa_crypto_tables_2026_09 --dry-run

    # Apply: drops only the tables that pass the zero-row guard; tables with
    # rows are skipped (not aborted) and reported so operators can re-audit.
    opentelemetry-instrument python -m \\
        data_manager.maintenance.drop_orphan_petrosa_crypto_tables_2026_09 --apply

    # Restrict to a subset (repeatable):
    ... --apply --table extraction_metadata

Exit codes:
    0  — success (all requested tables processed; any row-count guard trips
         are reported but do not fail the run in --dry-run mode)
    2  — MYSQL_URI not set
    3  — --apply mode and at least one requested table tripped the row-count
         guard (rows > 0); tables that passed the guard were still dropped
    4  — database error
    5  — invocation error (mutually-exclusive flags, unknown --table, etc.)

Every DROP is `DROP TABLE IF EXISTS` — idempotent; re-running after a
successful drop (or against a table already absent) is a no-op.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

TARGET_SCHEMA = "petrosa_crypto"

# Ordered so log output reads newest-evidence-first; order has no functional
# effect (each table is independently guarded).
TARGET_TABLES: tuple[str, ...] = ("extraction_metadata",)

EXISTS_SQL = (
    "SELECT TABLE_NAME FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
)


class RowCountGuardError(RuntimeError):
    """Raised (per-table, collected not re-raised by the runner) when a
    target table has rows and --apply was requested."""


def table_exists(engine: Engine, table: str, *, schema: str = TARGET_SCHEMA) -> bool:
    """Return True if `table` exists in `schema`."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(EXISTS_SQL),
            {"schema": schema, "table": table},
        ).fetchone()
    return row is not None


def count_rows(engine: Engine, table: str) -> int:
    """Return the row count of `table`. Caller guarantees `table` is a
    known constant from TARGET_TABLES — never user/network input — so a
    parameterized identifier is unnecessary here."""
    stmt = sa.text(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
    with engine.connect() as conn:
        row = conn.execute(stmt).fetchone()
    if row is None:
        return 0
    return int(row[0])


def drop_table(engine: Engine, table: str) -> None:
    """Execute the idempotent DROP TABLE statement for `table`."""
    stmt = sa.text(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
    with engine.begin() as conn:
        conn.execute(stmt)


def execute_migration_for_table(
    engine: Engine, table: str, *, dry_run: bool
) -> dict[str, object]:
    """Run the guarded-drop flow for a single table. Never raises —
    row-count guard trips are reported in the result dict; the caller
    decides whether that constitutes a failing exit code."""
    result: dict[str, object] = {
        "target_schema": TARGET_SCHEMA,
        "target_table": table,
        "dry_run": dry_run,
        "table_existed": False,
        "row_count": 0,
        "dropped": False,
        "guard_tripped": False,
        "sql_planned": f"DROP TABLE IF EXISTS {table}",
    }

    if not table_exists(engine, table):
        logger.info(
            "%s: %s.%s not present — nothing to do",
            __name__,
            TARGET_SCHEMA,
            table,
        )
        return result

    result["table_existed"] = True
    rows = count_rows(engine, table)
    result["row_count"] = rows

    if rows > 0:
        result["guard_tripped"] = True
        if dry_run:
            logger.warning(
                "%s: dry-run sees %d rows in %s — drop would be refused",
                __name__,
                rows,
                table,
            )
        else:
            logger.error(
                "%s: refusing to drop %s: row-count guard tripped (rows=%d); "
                "the orphan classification is no longer valid for this table",
                __name__,
                table,
                rows,
            )
        return result

    if dry_run:
        logger.info("%s: dry-run — would execute: %s", __name__, result["sql_planned"])
        return result

    drop_table(engine, table)
    result["dropped"] = True
    logger.info("%s: dropped %s", __name__, table)
    return result


def execute_migration(
    engine: Engine, *, dry_run: bool, tables: tuple[str, ...] = TARGET_TABLES
) -> list[dict[str, object]]:
    """Run the guarded-drop flow across `tables` (default: all six)."""
    return [
        execute_migration_for_table(engine, table, dry_run=dry_run) for table in tables
    ]


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.drop_orphan_petrosa_crypto_tables_2026_09",
        description=(
            "Guarded drop of the retained legacy petrosa_crypto MySQL table. "
            "See docs/persistence-architecture.md for the current policy."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned SQL + row count per table without modifying the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the drop to every table that passes the row-count guard.",
    )
    parser.add_argument(
        "--table",
        action="append",
        dest="tables",
        choices=TARGET_TABLES,
        default=None,
        help="Restrict to this table (repeatable). Default: extraction_metadata.",
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

    tables = tuple(args.tables) if args.tables else TARGET_TABLES

    try:
        engine = _make_engine_from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    try:
        results = execute_migration(engine, dry_run=args.dry_run, tables=tables)
    except SQLAlchemyError as exc:
        logger.error("database error during migration: %s", exc)
        return 4
    finally:
        engine.dispose()

    dropped = [r["target_table"] for r in results if r["dropped"]]
    guarded = [r["target_table"] for r in results if r["guard_tripped"]]
    logger.info(
        "migration summary: dropped=%s guarded(rows>0, skipped)=%s", dropped, guarded
    )

    if not args.dry_run and guarded:
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
