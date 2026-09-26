from pathlib import Path

ROOT = Path(__file__).parents[1]
S1 = (
    "MongoDB is the operational store: every live-path read and write goes to "
    "MongoDB. MySQL holds a historic reference copy only (statistical analysis, "
    "backtesting, research) and is never read on the live path."
)
S2 = (
    "`petrosa-data-manager` is the only service that connects to any database; "
    "every other service reads and writes data exclusively through the data-manager API."
)


def test_storage_pillars_are_canonical():
    architecture = (ROOT / "docs/persistence-architecture.md").read_text()
    cursorrules = (ROOT / ".cursorrules").read_text()
    assert S1 in architecture
    assert S2 in architecture
    assert S2 in cursorrules


def test_docs_do_not_call_mysql_the_system_of_record():
    for path in (ROOT / "docs").rglob("*.md"):
        if "archive" in path.parts or any("-20" in part for part in path.parts):
            continue
        text = path.read_text()
        assert "durable system of record" not in text
