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
    ttl_policy_for_collection,
    unregistered_collections,
)


def test_ticket_collections_are_classified():
    signals = entry_for_collection("signals")
    decisions = entry_for_collection("cio_decisions")
    funding = entry_for_collection("funding_rates_BTCUSDT")
    candles = entry_for_collection("candles_BTCUSDT_5m")
    analytics = entry_for_collection("analytics_BTCUSDT")
    klines = entry_for_collection("klines_15m")
    assert signals is not None and signals.classification == "durable"
    assert decisions is not None and decisions.mysql_table == "cio_decisions"
    assert funding is not None and funding.classification == "durable"
    assert candles is not None and candles.classification == "transient_only"
    assert analytics is not None and analytics.classification == "transient_only"
    assert klines is not None and klines.classification == "operational"
    assert klines.mysql_table == "klines_*"
    assert klines.min_retention == ">=400 candles per interval"


def test_operational_collections_are_protected():
    klines = entry_for_collection("klines_15m")
    assert klines is not None
    assert klines.classification in {"durable", "operational"}


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


def test_every_static_application_collection_has_ttl_policy():
    root = Path(__file__).parents[1] / "data_manager"
    collections = scan_mongo_collection_names(root)
    assert collections
    assert all(ttl_policy_for_collection(collection) for collection in collections)


def test_live_state_ttl_is_partial_and_does_not_expire_open_documents():
    policy = ttl_policy_for_collection("positions")
    assert policy is not None
    assert policy.field == "_ttl_expires_at"
    assert policy.partial_filter_expression == {"_ttl_expires_at": {"$exists": True}}


def test_config_rate_limits_uses_the_shared_timestamp_ttl_contract():
    policy = ttl_policy_for_collection("config_rate_limits")
    assert policy is not None
    assert policy.field == "timestamp"
    assert policy.seconds == 3600
    assert policy.partial_filter_expression is None


def test_performance_snapshots_uses_computed_at_ttl_contract():
    policy = ttl_policy_for_collection("performance_snapshots")
    assert policy is not None
    assert policy.field == "computed_at"
    assert policy.seconds > 0


def test_scanner_finds_names_passed_to_collection_helpers(tmp_path: Path):
    module = tmp_path / "repository.py"
    module.write_text(
        "class Repo:\n"
        "    def _collection(self, name):\n"
        "        return self.db[name]\n"
        "    async def bump(self):\n"
        '        await self._collection("hidden_counter").find_one_and_update({}, {})\n'
        '        await self.mongodb.get_collection("other_hidden").find_one({})\n',
        encoding="utf-8",
    )

    assert {"hidden_counter", "other_hidden"} <= scan_mongo_collection_names(tmp_path)
    assert unregistered_collections(tmp_path) == {"hidden_counter", "other_hidden"}


def test_trading_state_mysql_sequences_has_a_never_tripped_ttl():
    root = Path(__file__).parents[1] / "data_manager"
    assert "trading_state_mysql_sequences" in scan_mongo_collection_names(root)
    policy = ttl_policy_for_collection("trading_state_mysql_sequences")
    assert policy is not None
    assert policy.field == "_ttl_expires_at"
    assert policy.partial_filter_expression == {"_ttl_expires_at": {"$exists": True}}


def test_exchange_positions_has_a_never_tripped_ttl():
    """Live state written by tradeengine via the data-manager API, unregistered until #829.

    It is reached through a generic write endpoint, so the static scanner cannot see it. The TTL
    index only expires documents carrying ``_ttl_expires_at``, which nothing writes: live position
    state is never deleted.
    """
    for collection in (
        "exchange_positions",
        "strategy_positions",
        "position_contributions",
    ):
        policy = ttl_policy_for_collection(collection)
        assert policy is not None, collection
        assert policy.field == "_ttl_expires_at"
        assert policy.partial_filter_expression == {
            "_ttl_expires_at": {"$exists": True}
        }


def test_every_live_collection_measured_2026_11_05_has_a_ttl_policy():
    """All 214 collections listed live on 2026-11-05 (4 databases); 3 were unregistered (#829)."""
    tradeengine_written = {
        "exchange_positions",
        "strategy_positions",
        "position_contributions",
    }
    assert all(ttl_policy_for_collection(c) is not None for c in tradeengine_written)
