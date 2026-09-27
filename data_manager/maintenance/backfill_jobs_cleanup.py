"""Mark pre-#312 backfill jobs with no flattened data type as failed."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from sqlalchemy import func, select, update

from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

LEGACY_ERROR = "legacy_unflattened_row_pre_312"


def cleanup_legacy_jobs(mysql: MySQLAdapter, *, dry_run: bool = True) -> int:
    """Count, or mark, rows whose flattened ``data_type`` is empty.

    The operation is intentionally an UPDATE-only maintenance action.  Rows are
    retained for auditability and are never deleted.
    """
    table = mysql._get_table("backfill_jobs")
    engine = mysql._ensure_connected()
    predicate = table.c.data_type == ""
    with engine.begin() as conn:
        if dry_run:
            return int(
                conn.execute(
                    select(func.count()).select_from(table).where(predicate)
                ).scalar()
                or 0
            )

        result = conn.execute(
            update(table)
            .where(predicate)
            .values(
                status="failed",
                error_message=LEGACY_ERROR,
                completed_at=datetime.utcnow(),
            )
        )
        return int(result.rowcount or 0)


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.backfill_jobs_cleanup",
        description="Mark legacy unflattened backfill jobs as failed.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the UPDATE; without this flag only report the count.",
    )
    args = parser.parse_args(argv)
    mysql = MySQLAdapter(connection_string=os.getenv("MYSQL_URL"))
    mysql.connect()
    try:
        count = cleanup_legacy_jobs(mysql, dry_run=not args.apply)
        mode = "updated" if args.apply else "would update"
        logger.info("backfill_jobs_cleanup: %d rows %s", count, mode)
    finally:
        mysql.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
