"""Static MySQL 5.7 contract for the trades identifier migration."""

from pathlib import Path

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"
FORWARD = MIGRATIONS / "017_trades_bigint.sql"
ROLLBACK = MIGRATIONS / "017_trades_bigint_rollback.sql"


def test_trades_migration_has_guarded_bigint_changes_and_rollback():
    forward = FORWARD.read_text().upper()
    rollback = ROLLBACK.read_text().upper()

    assert "INFORMATION_SCHEMA.COLUMNS" in forward
    assert "MODIFY TRADE_ID BIGINT NOT NULL" in forward
    assert "MODIFY ORDER_ID BIGINT NULL" in forward
    assert "INPLACE" not in forward
    assert "ALGORITHM" not in forward
    assert "PREPARE TRADES_ALTER_STMT" in forward
    assert "MODIFY TRADE_ID INT NOT NULL" in rollback
    assert "MODIFY ORDER_ID INT NULL" in rollback
    assert "INPLACE" not in rollback
    assert "ALGORITHM" not in rollback


def test_each_trades_migration_uses_one_alter_statement():
    for path in (FORWARD, ROLLBACK):
        code = "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith("--")
        ).upper()
        assert code.count("ALTER TABLE") == 1
