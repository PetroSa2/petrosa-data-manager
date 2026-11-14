"""Every SQL migration must run on production's MySQL 5.7 (PetroSa2/petrosa-data-manager#880).

013 used MariaDB's ADD COLUMN IF NOT EXISTS. MySQL 5.7 rejects it, so its columns were never created and the
trading-state copy silently dropped those fields for weeks. This guard fails on syntax 5.7 does not accept.
013 itself is kept as history and listed as superseded by 020; it must not be run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).parents[1] / "data_manager" / "scripts" / "migrations"

# Known, superseded files that must never be run on 5.7. Each entry names its replacement.
SUPERSEDED = {
    "013_trading_state_mysql_copy.sql": "020_trading_state_columns_mysql57.sql",
}

# Files that never ran on 5.7 and are not replaced yet. Each one has its own ticket; remove the entry with the fix.
KNOWN_UNFIXED = {
    "003_klines_drop_redundant_index.sql": "PetroSa2/petrosa-data-manager#882",
}

# Syntax MySQL 5.7 rejects (MariaDB extensions or MySQL 8.0 additions).
NOT_ON_MYSQL_57 = {
    "ADD COLUMN/INDEX/KEY/CONSTRAINT IF NOT EXISTS": r"\bADD\s+(COLUMN|INDEX|KEY|UNIQUE(\s+(INDEX|KEY))?|CONSTRAINT)\s+IF\s+NOT\s+EXISTS\b",
    "DROP COLUMN/INDEX/KEY/CONSTRAINT IF EXISTS": r"\bDROP\s+(COLUMN|INDEX|KEY|CONSTRAINT|FOREIGN\s+KEY)\s+IF\s+EXISTS\b",
    "CREATE INDEX IF NOT EXISTS": r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b",
    "ALTER TABLE IF EXISTS": r"\bALTER\s+TABLE\s+IF\s+EXISTS\b",
    "MODIFY/CHANGE IF EXISTS": r"\b(MODIFY|CHANGE)\s+(COLUMN\s+)?IF\s+EXISTS\b",
    "RENAME COLUMN (8.0)": r"\bRENAME\s+COLUMN\b",
    "RETURNING": r"\bRETURNING\b",
    "CREATE SEQUENCE": r"\bCREATE\s+SEQUENCE\b",
    "CREATE OR REPLACE TABLE": r"\bCREATE\s+OR\s+REPLACE\s+TABLE\b",
    "WITH (CTE, 8.0)": r"^\s*WITH\s+\w+\s+AS\s*\(",
}


def _code(path: Path) -> str:
    """The file without comment lines, upper-cased."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    ).upper()


def _migrations() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def test_there_are_migrations_to_check():
    names = {path.name for path in _migrations()}
    assert "020_trading_state_columns_mysql57.sql" in names
    assert len(names) >= 20


@pytest.mark.parametrize("path", _migrations(), ids=lambda p: p.name)
def test_migration_uses_only_mysql_57_syntax(path: Path):
    if path.name in SUPERSEDED:
        pytest.skip(f"superseded by {SUPERSEDED[path.name]}; never run on 5.7")
    if path.name in KNOWN_UNFIXED:
        pytest.skip(f"never ran on 5.7; fix tracked in {KNOWN_UNFIXED[path.name]}")
    code = _code(path)
    hits = [
        name
        for name, pattern in NOT_ON_MYSQL_57.items()
        if re.search(pattern, code, re.M)
    ]
    assert hits == [], f"{path.name} uses syntax MySQL 5.7 rejects: {hits}"


def test_the_guard_catches_the_013_bug():
    """The patterns must actually match what broke 013, or this guard proves nothing."""
    code = _code(MIGRATIONS / "013_trading_state_mysql_copy.sql")
    pattern = NOT_ON_MYSQL_57["ADD COLUMN/INDEX/KEY/CONSTRAINT IF NOT EXISTS"]
    assert re.search(pattern, code)


def test_superseded_files_say_so_and_name_their_replacement():
    for name, replacement in SUPERSEDED.items():
        header = (MIGRATIONS / name).read_text().splitlines()[:6]
        assert any("SUPERSEDED" in line.upper() for line in header), name
        assert any(replacement in line for line in header), name
        assert (MIGRATIONS / replacement).is_file()


def test_known_unfixed_files_really_use_5_7_rejected_syntax():
    """An exception that no longer matches must be removed, so the list can't go stale."""
    for name in KNOWN_UNFIXED:
        code = _code(MIGRATIONS / name)
        assert any(re.search(p, code, re.M) for p in NOT_ON_MYSQL_57.values()), name
