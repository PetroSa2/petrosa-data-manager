"""
health_metrics retention maintenance job (AC2 of petrosa-data-manager#220).

Deletes health_metrics rows older than the configured retention window from
MySQL, preventing the table from growing unboundedly. Designed to be invoked
from a Kubernetes CronJob via:

    python -m data_manager.maintenance.health_metrics_retention [--dry-run]

The default retention window is 90 days, overridable via the
``HEALTH_METRICS_RETENTION_DAYS`` environment variable. The window was chosen
because health_metrics is operational/observability data: 90 days covers any
reasonable incident-review window while preventing unbounded MySQL growth
(the table was 143 MB with 393 k rows as of the 2026-06-06 audit).

See the umbrella storage-audit at https://github.com/PetroSa2/petrosa_k8s/issues/800
and https://github.com/PetroSa2/petrosa-data-manager/issues/220 for context.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from data_manager.db.mysql_adapter import MySQLAdapter

logger = logging.getLogger(__name__)

HEALTH_METRICS_TABLE = "health_metrics"
DEFAULT_RETENTION_DAYS = 90

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class RetentionConfig:
    """Resolved configuration for one retention run."""

    retention_days: int = DEFAULT_RETENTION_DAYS
    dry_run: bool = False


@dataclass
class RetentionResult:
    """Outcome of a single retention run."""

    table: str
    cutoff: datetime
    rows_deleted: int
    dry_run: bool


def compute_cutoff(now: datetime, days: int) -> datetime:
    """Return the exclusive upper bound on rows eligible for deletion."""
    return now - timedelta(days=days)


def load_config_from_env(environ: dict[str, str] | None = None) -> RetentionConfig:
    """Build a :class:`RetentionConfig` from environment variables."""
    env = environ if environ is not None else os.environ
    retention_days = _parse_int_env(
        env, "HEALTH_METRICS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, minimum=1
    )
    dry_run = env.get("HEALTH_METRICS_RETENTION_DRY_RUN", "").lower() == "true"
    return RetentionConfig(retention_days=retention_days, dry_run=dry_run)


def _parse_int_env(env: dict[str, str], key: str, default: int, *, minimum: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer %s=%r; using default %d", key, raw, default
        )
        return default
    if value < minimum:
        logger.warning("Clamping %s=%d to minimum %d", key, value, minimum)
        return minimum
    return value


def prune_health_metrics(
    adapter: MySQLAdapter,
    config: RetentionConfig,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete health_metrics rows older than ``config.retention_days`` days.

    In dry-run mode counts eligible rows without deleting them.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    cutoff = compute_cutoff(effective_now, config.retention_days)

    logger.info(
        "health_metrics_retention: cutoff=%s retention_days=%d%s",
        cutoff.isoformat(),
        config.retention_days,
        " (dry-run)" if config.dry_run else "",
    )

    if config.dry_run:
        rows_deleted = adapter.get_record_count(HEALTH_METRICS_TABLE, end=cutoff)
        logger.info(
            "health_metrics_retention: dry-run — would delete %d rows", rows_deleted
        )
    else:
        rows_deleted = adapter.delete_range(
            HEALTH_METRICS_TABLE, start=_EPOCH, end=cutoff
        )
        logger.info("health_metrics_retention: deleted %d rows", rows_deleted)

    return RetentionResult(
        table=HEALTH_METRICS_TABLE,
        cutoff=cutoff,
        rows_deleted=rows_deleted,
        dry_run=config.dry_run,
    )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_manager.maintenance.health_metrics_retention",
        description=(
            "Delete health_metrics rows older than the configured retention window."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would be deleted without modifying any data.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Override HEALTH_METRICS_RETENTION_DAYS for this run.",
    )
    return parser


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = _build_argparser()
    args = parser.parse_args(argv)

    config = load_config_from_env()
    if args.dry_run:
        config.dry_run = True
    if args.retention_days is not None:
        config.retention_days = max(1, args.retention_days)

    adapter = MySQLAdapter(connection_string=os.getenv("MYSQL_URL"))
    adapter.connect()
    try:
        result = prune_health_metrics(adapter, config)
        logger.info(
            "health_metrics_retention: complete — %d rows %s (cutoff=%s)",
            result.rows_deleted,
            "would-be-deleted" if result.dry_run else "deleted",
            result.cutoff.isoformat(),
        )
    finally:
        adapter.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
