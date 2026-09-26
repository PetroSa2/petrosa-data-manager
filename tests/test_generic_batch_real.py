"""POST /api/v1/{database}/{collection}/batch persists every sub-operation (#378, G3).

``update`` only changed in-memory dicts and ``delete`` only counted; both still
reported success. Now they reach the adapters, a malformed batch is rejected
before anything runs, and a failure part-way reports what already completed.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mysql_adapter import WriteResult
from data_manager.utils.circuit_breaker import CircuitBreakerOpenError


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def client(mock_db_manager):
    mongo = mock_db_manager.mongodb_adapter
    mongo.update = AsyncMock(return_value=2)
    mongo.delete_many = AsyncMock(return_value=1)
    mongo.write = AsyncMock(return_value=1)
    mysql = mock_db_manager.mysql_adapter
    mysql.get_column_names = Mock(return_value={"position_id", "status", "updated_at"})
    mysql.update = Mock(return_value=2)
    mysql.delete = Mock(return_value=1)
    mysql.write = Mock(return_value=WriteResult(inserted=1))
    api_module.db_manager = mock_db_manager
    yield TestClient(api_module.create_app())
    api_module.db_manager = None


def _batch(client, database, operations):
    return client.post(
        f"/api/v1/{database}/positions/batch", json={"operations": operations}
    )


UPDATE = {"type": "update", "filter": {"position_id": "p1"}, "data": {"status": "x"}}
DELETE = {"type": "delete", "filter": {"position_id": "p2"}}
INSERT = {"type": "insert", "data": [{"position_id": "p3", "status": "open"}]}


class TestMongoBatch:
    def test_update_and_delete_reach_the_adapter_without_scanning(self, client):
        mongo = api_module.db_manager.mongodb_adapter

        response = _batch(client, "mongodb", [UPDATE, DELETE])

        assert response.status_code == 200
        assert response.json()["results"] == [
            {"type": "update", "count": 2},
            {"type": "delete", "count": 1},
        ]
        mongo.update.assert_awaited_once()
        collection, filter_dict, data = mongo.update.await_args.args
        assert (collection, filter_dict) == ("positions", {"position_id": "p1"})
        assert data["status"] == "x"
        assert "updated_at" in data
        mongo.delete_many.assert_awaited_once_with("positions", {"position_id": "p2"})
        mongo.query_range.assert_not_called()

    def test_insert_keeps_the_record_fields(self, client):
        """GenericModel dropped every field, so batch inserts wrote empty documents."""
        mongo = api_module.db_manager.mongodb_adapter

        response = _batch(client, "mongodb", [INSERT])

        assert response.status_code == 200
        assert response.json()["results"] == [{"type": "insert", "count": 1}]
        models, collection = mongo.write.await_args.args
        record = models[0].model_dump()
        assert collection == "positions"
        assert record["position_id"] == "p3"
        assert record["status"] == "open"
        assert "timestamp" in record

    def test_unknown_type_is_400_and_runs_nothing(self, client):
        mongo = api_module.db_manager.mongodb_adapter

        response = _batch(client, "mongodb", [UPDATE, {"type": "upsert"}])

        assert response.status_code == 400
        assert "Batch operation 1" in response.json()["detail"]
        mongo.update.assert_not_called()

    @pytest.mark.parametrize(
        "bad_operation",
        [
            {"type": "delete", "filter": {}},
            {"type": "delete"},
            {"type": "update", "filter": {"position_id": {"$ne": 1}}, "data": {"a": 1}},
            {"type": "update", "filter": {"position_id": "p1"}},
            {"type": "insert", "data": ["not-an-object"]},
        ],
    )
    def test_invalid_operation_rejects_the_whole_batch_up_front(
        self, client, bad_operation
    ):
        mongo = api_module.db_manager.mongodb_adapter

        response = _batch(client, "mongodb", [INSERT, bad_operation])

        assert response.status_code == 400
        mongo.write.assert_not_called()
        mongo.update.assert_not_called()
        mongo.delete_many.assert_not_called()

    def test_failure_part_way_reports_completed_operations(self, client):
        mongo = api_module.db_manager.mongodb_adapter
        mongo.delete_many.side_effect = DatabaseError("mongo down")

        response = _batch(client, "mongodb", [UPDATE, DELETE, INSERT])

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["failed_operation_index"] == 1
        assert detail["completed_results"] == [{"type": "update", "count": 2}]
        assert "mongo down" in detail["message"]
        mongo.write.assert_not_called()
        api_module.db_manager.increment_error_count.assert_called_with("mongodb")

    def test_oversized_batch_is_400(self, client, monkeypatch):
        import constants

        monkeypatch.setattr(constants, "API_MAX_BATCH_SIZE", 1)

        response = _batch(client, "mongodb", [UPDATE, DELETE])

        assert response.status_code == 400

    def test_unsupported_database_is_400(self, client):
        response = _batch(client, "postgres", [UPDATE])

        assert response.status_code == 400


class TestMySQLBatch:
    def test_every_blocking_call_runs_off_the_event_loop(self, client):
        mysql = api_module.db_manager.mysql_adapter
        seen_on_loop = []

        def record(result):
            def side_effect(*_args, **_kwargs):
                seen_on_loop.append(_on_event_loop())
                return result

            return side_effect

        mysql.get_column_names.side_effect = record({"position_id", "status"})
        mysql.update.side_effect = record(2)
        mysql.delete.side_effect = record(1)
        mysql.write.side_effect = record(WriteResult(inserted=1))

        response = _batch(client, "mysql", [UPDATE, DELETE, INSERT])

        assert response.status_code == 200
        assert response.json()["results"] == [
            {"type": "update", "count": 2},
            {"type": "delete", "count": 1},
            {"type": "insert", "count": 1},
        ]
        assert len(seen_on_loop) == 4
        assert not any(seen_on_loop)
        mysql.query_range.assert_not_called()

    def test_update_payload_is_validated_before_anything_runs(self, client):
        mysql = api_module.db_manager.mysql_adapter
        bad_update = {
            "type": "update",
            "filter": {"position_id": "p1"},
            "data": {"no_such_column": 1},
        }

        response = _batch(client, "mysql", [DELETE, bad_update])

        assert response.status_code == 422
        mysql.delete.assert_not_called()
        mysql.update.assert_not_called()

    def test_open_circuit_part_way_is_503(self, client):
        mysql = api_module.db_manager.mysql_adapter
        mysql.delete.side_effect = CircuitBreakerOpenError("mysql_write", 30)

        response = _batch(client, "mysql", [UPDATE, DELETE])

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["failed_operation_index"] == 1
        assert detail["completed_results"] == [{"type": "update", "count": 2}]

    def test_column_lookup_failure_during_validation_is_500(self, client):
        mysql = api_module.db_manager.mysql_adapter
        mysql.get_column_names.side_effect = DatabaseError("cannot reflect")

        response = _batch(client, "mysql", [UPDATE])

        assert response.status_code == 500
        mysql.update.assert_not_called()
