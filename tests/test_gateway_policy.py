"""Generic gateway collection policy tests."""

from data_manager.api.gateway_policy import DENY_BY_DESIGN, check_generic
from data_manager.persistence_registry import REGISTRY


def test_collection_policy_examples():
    assert check_generic("mongodb", "klines_15m", "insert") is True
    assert check_generic("mongodb", "app_config", "delete") is False
    assert check_generic("mysql", "klines_m15", "read") is True
    assert check_generic("mysql", "signals", "insert") is False


def test_every_registry_entry_has_policy_decision():
    from data_manager.api.gateway_policy import GENERIC_POLICY

    for collection in REGISTRY:
        assert collection in GENERIC_POLICY["mongodb"] or collection in DENY_BY_DESIGN
