"""The exchange-truth stats snapshot collection is registered with a TTL (petrosa-data-manager#842)."""

from data_manager import persistence_registry as registry


def test_snapshot_collection_is_registered_as_recomputable():
    spec = registry.entry_for_collection("strategy_stats_snapshots")
    assert spec is not None
    assert spec.classification == "transient_only"


def test_snapshot_ttl_outlives_two_refresh_cycles():
    policy = registry.ttl_policy_for_collection("strategy_stats_snapshots")
    assert policy is not None
    assert policy.field == "computed_at"
    refresh_cycle_seconds = 15 * 60
    # staleness is decided by the serving logic (two missed cycles), never by a TTL gap
    assert policy.seconds > 2 * refresh_cycle_seconds
    assert policy.partial_filter_expression is None  # every snapshot expires
