"""Boot-time NATS publisher wiring contract (#197).

Validates that `data_manager/main.py` wires the operator-route NATS
publishers correctly at app start, that the same publisher instance is
shared between `envelopes.changed` and `cio.config.leverage_bounds.updated`
(AC3 — one connection, two subjects), and that shutdown unwires both
(AC5 — no stale half-closed publisher carried across restarts).

For AC6 (single accept-call → single emit observed by the wired
publisher), see `tests/unit/test_envelopes_api.py` — the route-level
emit-on-accept contract is exercised there with a recording publisher
that mirrors what `main.py` injects at boot. This test file proves the
boot-side of that contract: the wiring is real and the same publisher
flows to both subject owners.

Dynamic tests (`*_method_executes_*`) hit the extracted
`_wire_route_publishers` / `_unwire_route_publishers` methods on
`DataManagerApp` directly to close the codecov coverage gap that
inspect-based structural tests alone could not.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from data_manager.api.routes import envelopes, leverage_bounds
from data_manager.main import DataManagerApp


class _RecordingPublisher:
    """Records every publish() call. Mirrors the `_NATSPublisher` Protocol
    used by both route modules (`async publish(subject, payload) -> None`)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.calls.append((subject, payload))


def _reset_module_publishers() -> None:
    envelopes._publisher = None
    leverage_bounds._publisher = None


def test_both_setters_share_one_publisher_instance() -> None:
    """AC1 + AC2 + AC3: a single publisher object can be wired into both
    setters; both module-level globals reflect the same identity."""
    _reset_module_publishers()
    rec = _RecordingPublisher()

    envelopes.set_envelopes_changed_publisher(rec)
    leverage_bounds.set_leverage_bounds_publisher(rec)

    assert envelopes._publisher is rec
    assert leverage_bounds._publisher is rec
    assert envelopes._publisher is leverage_bounds._publisher


def test_stop_unwires_both_publishers() -> None:
    """AC5: passing `None` to each setter unwires the publisher — a
    subsequent restart starts from a clean slate, no stale closed-conn
    reference."""
    _reset_module_publishers()
    rec = _RecordingPublisher()
    envelopes.set_envelopes_changed_publisher(rec)
    leverage_bounds.set_leverage_bounds_publisher(rec)

    envelopes.set_envelopes_changed_publisher(None)
    leverage_bounds.set_leverage_bounds_publisher(None)

    assert envelopes._publisher is None
    assert leverage_bounds._publisher is None


def test_wired_publisher_receives_publish_calls_via_route_helper() -> None:
    """AC6 (boot-side): a publisher wired at boot is the same object that
    receives `await publish(subject, payload)` calls from the route's
    `_publish_envelopes_changed` helper. Combined with the existing
    test_envelopes_api.py emit-on-accept tests, this proves end-to-end
    that a boot-wired publisher observes route-side emits."""
    _reset_module_publishers()
    rec = _RecordingPublisher()
    envelopes.set_envelopes_changed_publisher(rec)

    payload = {
        "strategy_or_portfolio_key": "BTCUSDT",
        "envelope_version": 7,
        "change_id": "test-change",
    }

    async def _drive() -> None:
        ok = await envelopes._publish_envelopes_changed(payload)
        assert ok is True

    asyncio.run(_drive())

    assert len(rec.calls) == 1
    subject, _ = rec.calls[0]
    assert subject == envelopes.ENVELOPES_CHANGED_NATS_SUBJECT


def test_wire_route_publishers_method_executes_dynamic() -> None:
    """AC1 + AC2 + AC3 (dynamic): `DataManagerApp._wire_route_publishers`
    actually runs and wires both route module globals to the SAME publisher
    instance passed in. Provides line coverage for the boot wiring code
    that the inspect-based structural tests alone could not.
    """
    _reset_module_publishers()
    app = DataManagerApp()
    rec = _RecordingPublisher()

    app._wire_route_publishers(rec)

    assert envelopes._publisher is rec
    assert leverage_bounds._publisher is rec
    assert app._envelope_leverage_publisher is rec


def test_unwire_route_publishers_method_executes_dynamic() -> None:
    """AC5 (dynamic): `DataManagerApp._unwire_route_publishers` actually
    runs and resets both route module globals to None even when the app
    was previously wired with a publisher. Provides line coverage for the
    teardown code, including the try/except wrapper."""
    _reset_module_publishers()
    rec = _RecordingPublisher()
    envelopes.set_envelopes_changed_publisher(rec)
    leverage_bounds.set_leverage_bounds_publisher(rec)

    app = DataManagerApp()
    app._unwire_route_publishers()

    assert envelopes._publisher is None
    assert leverage_bounds._publisher is None


def test_main_module_start_delegates_to_wire_route_publishers() -> None:
    """AC4 (structure): `DataManagerApp.start()` source contains a call to
    the extracted wiring method, and the inline ``_DeferredNatsClient``
    construction that supplies its argument. Guards against a regression
    where #197's wiring is reverted but routes still appear to work
    (because tests inject publishers directly), masking the loss of the
    production cache-bust.
    """
    src = inspect.getsource(DataManagerApp.start)
    assert "_wire_route_publishers" in src
    assert "_DeferredNatsClient" in src


def test_main_module_stop_delegates_to_unwire_route_publishers() -> None:
    """AC5 (structure ordering): `DataManagerApp.stop()` calls the unwire
    method BEFORE `consumer.stop()` so the deferred nats_client lookup is
    no-oped before its underlying client closes."""
    src = inspect.getsource(DataManagerApp.stop)
    assert "_unwire_route_publishers" in src
    unwire_idx = src.index("self._unwire_route_publishers()")
    consumer_stop_idx = src.index("await self.consumer.stop()")
    assert unwire_idx < consumer_stop_idx, (
        "stop() must call _unwire_route_publishers() BEFORE consumer.stop() — "
        "see #197 AC5 rationale"
    )


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset module-level publishers around every test so order doesn't
    matter and one test's leak can't mask another's bug."""
    _reset_module_publishers()
    yield
    _reset_module_publishers()
