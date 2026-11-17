"""Every (method, path) in the app is declared exactly once (petrosa-data-manager#866).

Two routers declaring the same (method, path) silently shadow each other: only the router registered first
serves the path and the rest is dead code. This guard fails if a duplicate is ever added again.
"""

from __future__ import annotations

import collections

from data_manager.api.app import create_app


def test_every_method_path_is_declared_exactly_once() -> None:
    seen = collections.Counter()
    for route in create_app().routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        for method in methods:
            seen[(method, route.path)] += 1

    duplicates = {pair: count for pair, count in seen.items() if count > 1}
    assert not duplicates, f"duplicate (method, path) routes: {duplicates}"
