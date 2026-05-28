"""Regression guard for #178 — every consumer must construct cleanly from a
sync context (no running event loop).

Background: pre-#177, `MarketDataConsumer.__init__` called
`asyncio.get_event_loop().time()` to seed a stats timer. Under pytest-asyncio
1.4.0 on Python 3.11.15, that raised ``RuntimeError: There is no current event
loop in thread 'MainThread'`` because pytest-asyncio's new strict-loop semantics
do not implicitly create a loop in sync fixtures. PR #177 swapped that call to
``time.monotonic()``; this hardening PR (#178) does the same for every other
consumer's per-message timing and pins the per-test loop scope. These tests
ensure no future consumer reintroduces a sync-context `asyncio.get_event_loop()`
in its constructor — they intentionally run as **plain sync tests** (no
``@pytest.mark.asyncio``) so pytest-asyncio does NOT install a current loop for
the assertion.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

# (module_path, class_name) pairs for every consumer that owns timing state.
_CONSUMER_CLASSES = [
    ("data_manager.consumer.market_data_consumer", "MarketDataConsumer"),
    ("data_manager.consumer.execution_events_consumer", "ExecutionEventsConsumer"),
    ("data_manager.consumer.intent_consumer", "IntentConsumer"),
    ("data_manager.consumer.decision_consumer", "DecisionConsumer"),
    ("data_manager.consumer.pnl_consumer", "PnlConsumer"),
]


@pytest.mark.parametrize(("module_path", "class_name"), _CONSUMER_CLASSES)
def test_consumer_constructs_without_running_event_loop(module_path, class_name):
    """Consumer __init__ must not require a running event loop (issue #178).

    Any sync-context call to ``asyncio.get_event_loop()`` would raise here
    under pytest-asyncio 1.x semantics, surfacing the regression immediately.
    """
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    # Pass MagicMocks for the optional collaborators so we do not pull in real
    # NATS / Mongo plumbing — we are only exercising the constructor's
    # synchronous setup code path.
    instance = cls(
        nats_client=MagicMock(),
        db_manager=MagicMock(),
    )
    assert instance is not None
    # Light sanity touch on the constructed object so any partial-init silently
    # swallowed by a try/except would surface here.
    assert hasattr(instance, "nats_client")
