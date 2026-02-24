#!/usr/bin/env python3
"""
Pre-commit hook to detect tests without assertions.

This script uses AST parsing to find test functions and verify they contain
assertion statements or expected patterns (pytest.raises with exc_info, etc.).
"""

import ast
import os
import subprocess
import sys
from typing import Any


class TestAssertionChecker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.test_functions: list[tuple[str, int, bool]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        is_test = node.name.startswith("test_") or node.name.endswith("_test")
        if is_test:
            has_assertion = any(self._has_assertion_pattern(child) for child in ast.walk(node))
            self.test_functions.append((node.name, node.lineno, has_assertion))
        self.generic_visit(node)

    def _has_assertion_pattern(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    func = item.context_expr.func
                    if isinstance(func, ast.Attribute) and func.attr in ("raises", "patch", "patch.object"):
                        return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr.startswith("assert"):
                return True
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "fail"
                and isinstance(func.value, ast.Name)
                and func.value.id == "pytest"
            ):
                return True
        return False


def find_test_files(paths: list[str] | None = None) -> list[str]:
    if paths:
        return [
            p
            for p in paths
            if os.path.isfile(p)
            and p.endswith(".py")
            and (os.path.basename(p).startswith("test_") or os.path.basename(p).endswith("_test.py"))
        ]

    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            check=True,
        )
        staged_files = result.stdout.strip().split("\n")
        return [
            f
            for f in staged_files
            if f.endswith(".py")
            and (
                os.path.basename(f).startswith("test_")
                or os.path.basename(f).endswith("_test.py")
                or "test" in os.path.dirname(f).lower()
            )
        ]
    except Exception:
        test_files: list[str] = []
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                if file.startswith("test_") and file.endswith(".py"):
                    test_files.append(os.path.join(root, file))
        return test_files


def check_file(filepath: str) -> tuple[bool, list[tuple[str, int]]]:
    try:
        with open(filepath, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=filepath)
        checker = TestAssertionChecker()
        checker.visit(tree)
        missing = [(name, line) for name, line, has_assert in checker.test_functions if not has_assert]
        return len(missing) == 0, missing
    except SyntaxError as e:
        print(f"⚠️  Syntax error in {filepath}:{e.lineno}: {e.msg}", file=sys.stderr)
        return True, []
    except Exception as e:
        print(f"❌ Error checking {filepath}: {e}", file=sys.stderr)
        return False, []


def main() -> None:
    files = find_test_files(sys.argv[1:] if len(sys.argv) > 1 else None)
    if not files:
        sys.exit(0)

    all_passed = True
    failed_tests: list[tuple[str, str, int]] = []
    for filepath in files:
        if not os.path.exists(filepath):
            continue
        passed, missing = check_file(filepath)
        if not passed:
            all_passed = False
            for test_name, line in missing:
                failed_tests.append((filepath, test_name, line))

    if not all_passed:
        print("❌ Tests without assertions detected:", file=sys.stderr)
        for filepath, test_name, line in failed_tests:
            print(f"  {filepath}:{line} - {test_name}()", file=sys.stderr)
        sys.exit(1)

    print("✅ All tests have assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()
