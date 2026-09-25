"""Tests for the machine-checked MongoDB persistence policy."""

from __future__ import annotations

from pathlib import Path

from data_manager.maintenance.drop_orphan_petrosa_crypto_tables_2026_09 import (
    TARGET_TABLES,
)
from data_manager.persistence_registry import (
    durable_mysql_tables,
    entry_for_collection,
    scan_mongo_collection_names,
    unregistered_collections,
)


def test_ticket_collections_are_classified():
    signals = entry_for_collection("signals")
    decisions = entry_for_collection("cio_decisions")
    funding = entry_for_collection("funding_rates_BTCUSDT")
    candles = entry_for_collection("candles_BTCUSDT_5m")
    analytics = entry_for_collection("analytics_BTCUSDT")
    assert signals is not None and signals.classification == "durable"
    assert decisions is not None and decisions.mysql_table == "cio_decisions"
    assert funding is not None and funding.classification == "durable"
    assert candles is not None and candles.classification == "transient_only"
    assert analytics is not None and analytics.classification == "transient_only"


def test_durable_tables_are_available_to_maintenance_guards():
    assert {"signals", "cio_decisions", "funding_rates"} <= durable_mysql_tables()
    assert "extraction_metadata" not in durable_mysql_tables()
    assert set(TARGET_TABLES) & durable_mysql_tables() == set()


def test_scanner_fails_for_an_unregistered_collection(tmp_path: Path):
    module = tmp_path / "writer.py"
    module.write_text(
        'COLLECTION = "unregistered_collection"\n'
        "async def write(db):\n"
        '    await db[COLLECTION].insert_one({"ok": True})\n',
        encoding="utf-8",
    )

    assert "unregistered_collection" in scan_mongo_collection_names(tmp_path)
    assert unregistered_collections(tmp_path) == {"unregistered_collection"}


def test_real_data_manager_has_no_unregistered_static_collections():
    root = Path(__file__).parents[1] / "data_manager"
    assert unregistered_collections(root) == set()
