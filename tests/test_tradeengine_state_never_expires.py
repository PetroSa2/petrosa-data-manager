"""Tradeengine position state written through the data API never carries the TTL field (#829).

exchange_positions, strategy_positions and position_contributions have a never-tripped TTL index on
``_ttl_expires_at``: it can only expire a document that carries that field. These writes must not.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.persistence_registry import ttl_policy_for_collection


@pytest.fixture
def client(mock_db_manager):
    mock_db_manager.mongodb_adapter = Mock()
    mock_db_manager.mongodb_adapter.write = AsyncMock(return_value=1)
    mock_db_manager.mysql_adapter = Mock()
    app = api_module.create_app()
    api_module.db_manager = mock_db_manager
    yield TestClient(app), mock_db_manager.mongodb_adapter
    api_module.db_manager = None


@pytest.mark.parametrize(
    ("collection", "record"),
    [
        (
            "exchange_positions",
            {
                "exchange_position_key": "BTCUSDT:LONG",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "status": "open",
                "current_quantity": 0.01,
                "contributing_strategies": ["iceberg_detector"],
            },
        ),
        (
            "strategy_positions",
            {"strategy_position_id": "sp-1", "strategy_id": "iceberg_detector"},
        ),
        (
            "position_contributions",
            {"contribution_id": "c-1", "exchange_position_key": "BTCUSDT:LONG"},
        ),
    ],
)
def test_tradeengine_state_write_never_carries_the_ttl_field(
    client, collection, record
):
    http, mongo = client
    policy = ttl_policy_for_collection(collection)
    assert policy is not None and policy.field == "_ttl_expires_at"

    response = http.post(
        "/api/v1/data/insert",
        json={"database": "mongodb", "collection": collection, "records": [record]},
    )

    assert response.status_code == 200, response.text
    written = [
        doc
        for call in mongo.write.await_args_list
        for doc in (call.args[0] if call.args else call.kwargs.get("data", []))
    ]
    assert written, "the insert must reach the Mongo adapter"
    for doc in written:
        fields = (
            doc if isinstance(doc, dict) else getattr(doc, "model_dump", lambda: {})()
        )
        assert policy.field not in fields
