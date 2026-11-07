"""PetroSa2/petrosa-data-manager#880: aliases are not "dropped", and every dropped field is named on the counter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data_manager.api.routes import trading_state as module

# The production positions columns after migration 020 (2026-11-07 shape plus the six new columns).
COLUMNS = {
    "id", "position_id", "strategy_id", "exchange", "symbol", "position_side", "entry_price", "quantity",
    "entry_time", "stop_loss", "take_profit", "status", "metadata", "exchange_position_id", "entry_order_id",
    "entry_trade_ids", "stop_loss_order_id", "take_profit_order_id", "commission_asset", "commission_total",
    "exit_price", "exit_time", "exit_order_id", "exit_trade_ids", "pnl", "pnl_pct", "pnl_after_fees",
    "duration_seconds", "close_reason", "final_commission", "created_at", "updated_at", "pnl_unknown",
    "entry_commission", "fee_status", "closed_by_strategy_id", "close_actor", "idempotency_key", "decision_id",
}  # fmt: skip
BEFORE_020 = COLUMNS - {
    "entry_commission",
    "fee_status",
    "closed_by_strategy_id",
    "close_actor",
    "idempotency_key",
    "decision_id",
}

# A tradeengine position document as stored in Mongo on 2026-11-07 (values from a production row).
DOCUMENT = {
    "position_id": "1a2a86e3-f7d1-44",
    "status": "closed",
    "symbol": "BTCUSDT",
    "entry_price": 75705.7,
    "avg_price": 75705.7,
    "entry_order_id": "28668113172",
    "order_id": "28668113172",
    "entry_trade_ids": ["554836984"],
    "trade_id": "554836984",
    "commission_total": 0.02119759,
    "commission": 0.02119759,
    "entry_commission": 0.02119759,
    "commission_asset": "USDT",
    "fee_asset": "USDT",
    "fee_status": "known",
    "decision_id": "349c114e453341de8fd4a32177d4b5e5",
    "idempotency_key": "28668113172:554836984",
    "close_actor": "reconciler",
    "closed_by_strategy_id": "s1",
    "updated_at": "2026-11-07T23:42:31+00:00",
    "last_update": "2026-11-06T06:44:13+00:00",
    "_mysql_copy_version": 7,
}


class _MySQL:
    def __init__(self, columns: set[str]):
        self.columns = columns
        self.written: list[dict] = []

    def get_column_names(self, table):
        return self.columns

    def persist_trading_state(self, table, data, version):
        self.written.append(data)


@pytest.fixture
def copy_env(monkeypatch):
    counter = MagicMock()
    monkeypatch.setattr(module, "TRADING_STATE_MYSQL_FIELDS_DROPPED", counter)
    monkeypatch.setattr(module, "_DROPPED_FIELD_NAMES", set())

    def run(columns: set[str], document: dict) -> tuple[_MySQL, dict[str, int]]:
        mysql = _MySQL(columns)
        monkeypatch.setattr(
            module.api_module, "db_manager", SimpleNamespace(mysql_adapter=mysql)
        )
        assert module._copy_to_mysql("positions", dict(document)) is True
        labels: dict[str, int] = {}
        for call in counter.add.call_args_list:
            labels[call.args[1]["field"]] = labels.get(call.args[1]["field"], 0) + 1
            assert call.args[1]["table"] == "positions"
        counter.reset_mock()
        return mysql, labels

    return run


def test_after_020_nothing_is_dropped_and_every_column_is_copied(copy_env):
    mysql, dropped = copy_env(COLUMNS, DOCUMENT)

    assert dropped == {}
    written = mysql.written[0]
    for field in (
        "fee_status",
        "entry_commission",
        "close_actor",
        "closed_by_strategy_id",
        "decision_id",
        "idempotency_key",
    ):
        assert written[field] == DOCUMENT[field]
    assert not set(written) & set(
        module._ALIASED_FIELDS
    )  # aliases never reach the SQL write


def test_before_020_the_missing_columns_are_named_not_other(copy_env):
    _, dropped = copy_env(BEFORE_020, DOCUMENT)

    assert dropped == {
        "entry_commission": 1,
        "fee_status": 1,
        "closed_by_strategy_id": 1,
        "close_actor": 1,
        "idempotency_key": 1,
        "decision_id": 1,
    }


def test_an_alias_without_its_target_value_is_counted_as_dropped(copy_env):
    document = {**DOCUMENT, "entry_order_id": None, "commission_total": None}
    document = {k: v for k, v in document.items() if v is not None}

    _, dropped = copy_env(COLUMNS, document)

    assert dropped == {"order_id": 1, "commission": 1}


def test_an_alias_whose_target_column_is_missing_is_counted(copy_env):
    _, dropped = copy_env(COLUMNS - {"commission_asset"}, DOCUMENT)

    # Both are lost: the column itself, and the alias that relied on it.
    assert dropped == {"fee_asset": 1, "commission_asset": 1}


def test_field_names_are_capped_to_protect_the_series_budget(copy_env, monkeypatch):
    monkeypatch.setattr(module, "_DROPPED_FIELD_NAME_CAP", 2)
    document = {
        "position_id": "p1",
        "status": "open",
        "_mysql_copy_version": 1,
        "unknown_a": 1,
        "unknown_b": 2,
        "unknown_c": 3,
        "Bad-Name": 4,
    }

    _, dropped = copy_env({"position_id", "status"}, document)

    assert sum(dropped.values()) == 4
    assert dropped["other_overflow"] == 2
    assert len([name for name in dropped if name.startswith("unknown_")]) == 2


def test_aliases_point_at_real_columns():
    assert set(module._ALIASED_FIELDS.values()) <= COLUMNS
    assert not set(module._ALIASED_FIELDS) & COLUMNS
