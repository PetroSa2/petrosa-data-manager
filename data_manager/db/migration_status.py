"""Read-only status checks for operator-run schema migrations."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

OPERATOR_MIGRATIONS = (
    "014_ledger_exchange_store",
    "015_ledger_adjustments_audit",
    "016_signals_point_in_time",
)


def unapplied_migrations(engine: Any) -> list[str]:
    """Return tracked operator migrations absent from the completion table."""
    table_exists = sa.text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = 'schema_migrations'"
    )
    with engine.connect() as connection:
        if connection.execute(table_exists).first() is None:
            return list(OPERATOR_MIGRATIONS)

    statement = sa.text(
        "SELECT migration_id FROM schema_migrations "
        "WHERE migration_id IN :migration_ids"
    ).bindparams(sa.bindparam("migration_ids", expanding=True))
    with engine.connect() as connection:
        applied = {
            row[0]
            for row in connection.execute(
                statement, {"migration_ids": list(OPERATOR_MIGRATIONS)}
            )
        }
    return [migration for migration in OPERATOR_MIGRATIONS if migration not in applied]
