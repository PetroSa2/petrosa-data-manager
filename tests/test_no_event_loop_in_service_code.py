"""Guard: service code never runs a coroutine on a loop of its own (#837).

The ledger tie-out ran ``asyncio.run(<Motor coroutine>)`` inside ``asyncio.to_thread``; Motor's client is
bound to the main loop, so every cycle failed with "Future attached to a different loop". Only the CLI
entrypoints (``data_manager/main.py`` and the ``maintenance`` scripts) may own an event loop.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1] / "data_manager"
ALLOWED = {ROOT / "main.py"}
ALLOWED_DIRS = {ROOT / "maintenance"}
FORBIDDEN_CALLS = {
    "run",
    "new_event_loop",
    "run_until_complete",
    "run_coroutine_threadsafe",
}


def _loop_owning_calls(path: Path, base: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        is_asyncio = isinstance(owner, ast.Name) and owner.id == "asyncio"
        if node.func.attr in FORBIDDEN_CALLS and (
            is_asyncio or node.func.attr == "run_until_complete"
        ):
            found.append(f"{path.relative_to(base)}:{node.lineno} {node.func.attr}")
    return found


def test_service_code_never_owns_an_event_loop():
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        if path in ALLOWED or any(parent in ALLOWED_DIRS for parent in path.parents):
            continue
        offenders.extend(_loop_owning_calls(path, ROOT.parent))
    assert offenders == [], (
        "await coroutines on the running loop instead of starting another one: "
        + ", ".join(offenders)
    )


def test_guard_detects_the_ledger_pattern(tmp_path):
    module = tmp_path / "repo.py"
    module.write_text(
        "import asyncio\n"
        "class Repo:\n"
        "    def ids(self):\n"
        "        return asyncio.run(self.live_ids())\n",
        encoding="utf-8",
    )
    assert _loop_owning_calls(module, tmp_path) == ["repo.py:4 run"]
