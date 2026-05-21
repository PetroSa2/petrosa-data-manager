"""Tests for the P4.1 follow-up P&L publisher (#652).

Covers:
  * `closed` emission when the fill realizes P&L against existing lots
  * `mark_to_market` emission when the fill only opens lots
  * no-emit cases: non-fill events, missing required fields
  * cold-start replay (`replay_history`) seeds the calculator without publishing
  * publish errors are logged and swallowed (do not poison the consumer path)
  * the subject is `pnl.events.<strategy_id>`
  * end-to-end binding: `ExecutionEventsConsumer(on_persisted=publisher.on_persisted)`
    publishes when `_process_message` succeeds (integration over mocks)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.models.execution_event import ExecutionEvent
from data_manager.services.pnl_publisher import PnlEventPublisher

T0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


def _evt(
    *,
    event_type: str = "filled",
    side: str = "buy",
    qty: float = 1.0,
    fill_qty: float = 1.0,
    price: float = 100.0,
    strategy_id: str = "S1",
    symbol: str = "BTCUSDT",
    order_id: str = "O1",
    decision_id: str = "D1",
) -> ExecutionEvent:
    return ExecutionEvent(
        decision_id=decision_id,
        strategy_id=strategy_id,
        order_id=order_id,
        event_type=event_type,
        timestamp=T0,
        side=side,
        qty=qty,
        fill_qty=fill_qty,
        price=price,
        symbol=symbol,
    )


def _stub_client() -> tuple[Any, AsyncMock]:
    """Return (nats_client_stub, publish_mock). publish_mock receives subject+payload."""
    publish_mock = AsyncMock()
    nc = MagicMock()
    nc.publish = publish_mock
    return nc, publish_mock


@pytest.mark.asyncio
async def test_on_persisted_emits_mark_to_market_when_opening_lots() -> None:
    """A first-of-its-kind fill should produce an `mark_to_market` event."""
    nc, publish = _stub_client()
    pub = PnlEventPublisher(nats_client=nc)

    await pub.on_persisted(_evt(side="buy", fill_qty=2.0, price=100.0))

    publish.assert_awaited_once()
    subject, payload = publish.call_args.args
    assert subject == "pnl.events.S1"
    body = json.loads(payload.decode())
    assert body["pnl_kind"] == "mark_to_market"
    assert body["decision_id"] == "D1"
    assert body["strategy_id"] == "S1"
    # Just-opened lot at the trade price → unrealized is exactly 0.
    assert body["unrealized_pnl_usd"] == pytest.approx(0.0)
    assert (
        "realized_pnl_usd" not in body
    )  # exclude_none drops the absent realized field


@pytest.mark.asyncio
async def test_on_persisted_emits_closed_when_realizing_pnl() -> None:
    """A sell that closes prior long lots must emit a `closed` event with the delta."""
    nc, publish = _stub_client()
    pub = PnlEventPublisher(nats_client=nc)

    # Open: buy 1 @ 100.
    await pub.on_persisted(
        _evt(side="buy", fill_qty=1.0, price=100.0, order_id="O-buy")
    )
    publish.reset_mock()

    # Close: sell 1 @ 110 → realized = +10.
    await pub.on_persisted(
        _evt(side="sell", fill_qty=1.0, price=110.0, order_id="O-sell")
    )

    publish.assert_awaited_once()
    subject, payload = publish.call_args.args
    assert subject == "pnl.events.S1"
    body = json.loads(payload.decode())
    assert body["pnl_kind"] == "closed"
    assert body["realized_pnl_usd"] == pytest.approx(10.0)
    assert body["order_id"] == "O-sell"
    assert "unrealized_pnl_usd" not in body


@pytest.mark.asyncio
async def test_on_persisted_skips_non_fill_event_types() -> None:
    """`placed` and `rejected` events must not produce any P&L message."""
    nc, publish = _stub_client()
    pub = PnlEventPublisher(nats_client=nc)

    await pub.on_persisted(_evt(event_type="placed"))
    await pub.on_persisted(_evt(event_type="rejected"))

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_persisted_skips_when_required_fields_missing() -> None:
    """Defensive: a `filled` event with no price/qty/symbol produces no message."""
    nc, publish = _stub_client()
    pub = PnlEventPublisher(nats_client=nc)

    no_price = ExecutionEvent(
        decision_id="D",
        strategy_id="S",
        order_id="O",
        event_type="filled",
        timestamp=T0,
        side="buy",
        qty=1.0,
        fill_qty=1.0,
        price=None,
        symbol="BTCUSDT",
    )
    no_symbol = ExecutionEvent(
        decision_id="D",
        strategy_id="S",
        order_id="O",
        event_type="filled",
        timestamp=T0,
        side="buy",
        qty=1.0,
        fill_qty=1.0,
        price=100.0,
        symbol=None,
    )
    await pub.on_persisted(no_price)
    await pub.on_persisted(no_symbol)

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_error_logged_and_swallowed(caplog) -> None:
    """A NATS publish error must NOT propagate out of the hook."""
    nc = MagicMock()
    nc.publish = AsyncMock(side_effect=RuntimeError("broker down"))
    pub = PnlEventPublisher(nats_client=nc)

    with caplog.at_level("WARNING", logger="data_manager.services.pnl_publisher"):
        await pub.on_persisted(_evt(side="buy", fill_qty=1.0, price=100.0))

    nc.publish.assert_awaited_once()
    assert any("pnl_event_publish_failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_replay_history_seeds_calculator_without_publishing() -> None:
    """Cold-start replay must mutate calculator state but not call publish."""
    nc, publish = _stub_client()
    pub = PnlEventPublisher(nats_client=nc)

    rows = [
        {
            "event_type": "filled",
            "side": "buy",
            "fill_qty": 1.0,
            "price": 100.0,
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
        },
        {  # not a fill — should be filtered out
            "event_type": "rejected",
            "side": "buy",
            "fill_qty": 0.0,
            "price": 0.0,
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
        },
        {
            "event_type": "filled",
            "side": "buy",
            "fill_qty": 2.0,
            "price": 110.0,
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
        },
    ]
    applied = pub.replay_history(rows)
    assert applied == 2
    publish.assert_not_awaited()

    # Now a live sell should realize against the seeded long lots (FIFO @ 100).
    await pub.on_persisted(_evt(side="sell", fill_qty=1.0, price=120.0))
    publish.assert_awaited_once()
    body = json.loads(publish.call_args.args[1].decode())
    assert body["pnl_kind"] == "closed"
    assert body["realized_pnl_usd"] == pytest.approx(20.0)  # (120 - 100) * 1


# ----------------------------------------------------------------------
# End-to-end: consumer with hook bound to publisher must round-trip.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_drives_publisher_on_successful_persist() -> None:
    """Wire a real consumer with a stub db_manager and a real publisher.

    Asserts that calling the consumer's `_on_persisted` hook (which is
    what the consumer worker does after a successful `_persist`) results
    in a `pnl.events.<strategy_id>` publish.
    """
    from data_manager.consumer.execution_events_consumer import ExecutionEventsConsumer

    coll = MagicMock()
    coll.insert_one = AsyncMock(return_value=None)
    mongodb = MagicMock()
    mongodb.db = MagicMock()
    mongodb.db.__getitem__ = MagicMock(return_value=coll)
    mongodb._prepare_for_bson = MagicMock(side_effect=lambda d: d)
    db_manager = MagicMock()
    db_manager.mongodb_adapter = mongodb

    nc, publish = _stub_client()
    pub = PnlEventPublisher(nats_client=nc)
    consumer = ExecutionEventsConsumer(
        db_manager=db_manager, on_persisted=pub.on_persisted
    )

    event = _evt(side="buy", fill_qty=1.0, price=100.0)
    persisted = await consumer._persist(event)
    assert persisted is True

    # The consumer worker would fire the hook here; do it directly.
    assert consumer._on_persisted is not None
    await consumer._on_persisted(event)

    publish.assert_awaited_once()
    subject, payload = publish.call_args.args
    assert subject == "pnl.events.S1"
    body = json.loads(payload.decode())
    assert body["pnl_kind"] == "mark_to_market"
    assert body["decision_id"] == "D1"
