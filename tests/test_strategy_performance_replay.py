"""Tests for close-event replay compatibility."""

from datetime import UTC, datetime

from data_manager.services.round_book import replay_book


def test_round_book_replays_position_closed_event():
    rows = [
        {
            "event_type": "filled",
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
            "side": "buy",
            "fill_qty": 1,
            "price": 100,
            "timestamp": datetime(2026, 10, 1, tzinfo=UTC),
        },
        {
            "event_type": "position_closed",
            "strategy_id": "S1",
            "symbol": "BTCUSDT",
            "side": "sell",
            "closed_quantity": 1,
            "exit_price": 110,
            "timestamp": datetime(2026, 10, 1, 1, tzinfo=UTC),
        },
    ]
    book = replay_book(rows)
    assert len(book.closed) == 1
    assert book.closed[0].realized == 10
