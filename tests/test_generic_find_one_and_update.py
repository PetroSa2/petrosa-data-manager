"""POST /api/v1/{database}/{collection}/find-one-and-update: equality CAS (#378).

The caller puts the expected current values in ``filter``; ``set`` lands only
if a document still matches them, atomically, in one ``findOneAndUpdate``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

import data_manager.api.app as api_module
from data_manager.db.base_adapter import DatabaseError
from data_manager.db.mongodb_adapter import MongoDBAdapter

URL = "/api/v1/{database}/heartbeat_state/find-one-and-update"


@pytest.fixture
def client(mock_db_manager):
    mock_db_manager.mongodb_adapter.find_one_and_update = AsyncMock(
        return_value={"service": "te", "version": 4}
    )
    api_module.db_manager = mock_db_manager
    yield TestClient(api_module.create_app())
    api_module.db_manager = None


def _cas(client, body, database="mongodb"):
    return client.post(URL.format(database=database), json=body)


class TestRoute:
    def test_matched_filter_returns_the_updated_document(self, client):
        mongo = api_module.db_manager.mongodb_adapter

        response = _cas(
            client, {"filter": {"service": "te", "version": 3}, "set": {"version": 4}}
        )

        assert response.status_code == 200
        assert response.json() == {
            "matched": True,
            "document": {"service": "te", "version": 4},
        }
        mongo.find_one_and_update.assert_awaited_once_with(
            "heartbeat_state",
            {"service": "te", "version": 3},
            {"version": 4},
            upsert=False,
        )
        api_module.db_manager.increment_query_count.assert_called_with("mongodb")

    def test_unmatched_filter_without_upsert_is_not_matched(self, client):
        api_module.db_manager.mongodb_adapter.find_one_and_update.return_value = None

        response = _cas(
            client,
            {"filter": {"service": "te", "version": 2}, "set": {"version": 3}},
        )

        assert response.status_code == 200
        assert response.json() == {"matched": False, "document": None}

    def test_upsert_flag_is_forwarded(self, client):
        mongo = api_module.db_manager.mongodb_adapter

        _cas(
            client,
            {"filter": {"service": "te"}, "set": {"version": 1}, "upsert": True},
        )

        assert mongo.find_one_and_update.await_args.kwargs == {"upsert": True}

    @pytest.mark.parametrize(
        "filter_dict", [{"$where": "true"}, {"version": {"$lt": 5}}]
    )
    def test_operator_filter_is_400(self, client, filter_dict):
        response = _cas(client, {"filter": filter_dict, "set": {"version": 4}})

        assert response.status_code == 400
        api_module.db_manager.mongodb_adapter.find_one_and_update.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [
            {"filter": {}, "set": {"version": 4}},
            {"filter": {"service": "te"}, "set": {}},
        ],
    )
    def test_empty_filter_or_set_is_400(self, client, body):
        response = _cas(client, body)

        assert response.status_code == 400
        api_module.db_manager.mongodb_adapter.find_one_and_update.assert_not_called()

    def test_mysql_is_400(self, client):
        response = _cas(
            client, {"filter": {"id": 1}, "set": {"status": "x"}}, database="mysql"
        )

        assert response.status_code == 400
        assert "MongoDB-only" in response.json()["detail"]

    def test_no_database_manager_is_503(self, client):
        api_module.db_manager = None

        response = _cas(client, {"filter": {"service": "te"}, "set": {"v": 1}})

        assert response.status_code == 503

    def test_missing_mongo_adapter_is_503(self, client):
        api_module.db_manager.mongodb_adapter = None

        response = _cas(client, {"filter": {"service": "te"}, "set": {"v": 1}})

        assert response.status_code == 503

    def test_driver_failure_is_500_not_a_client_error(self, client):
        """A transient Mongo failure must not look like a 400 the caller won't retry."""
        api_module.db_manager.mongodb_adapter.find_one_and_update.side_effect = (
            DatabaseError("Failed to compare-and-set heartbeat_state: timeout")
        )

        response = _cas(client, {"filter": {"service": "te"}, "set": {"v": 1}})

        assert response.status_code == 500
        api_module.db_manager.increment_error_count.assert_called_with("mongodb")

    @pytest.mark.parametrize(("upsert", "op"), [(False, "update"), (True, "upsert")])
    def test_route_goes_through_the_gateway_policy(self, client, upsert, op):
        with patch(
            "data_manager.api.routes.generic.authorize_generic"
        ) as authorize_generic:
            _cas(
                client,
                {"filter": {"service": "te"}, "set": {"v": 1}, "upsert": upsert},
            )

        authorize_generic.assert_called_once()
        assert authorize_generic.call_args.args[1:] == (
            "mongodb",
            "heartbeat_state",
            op,
        )

    def test_policy_denial_is_enforced(self, client, monkeypatch):
        monkeypatch.setattr(
            "data_manager.api.gateway_policy.auth_mode", lambda: "enforce"
        )

        response = _cas(client, {"filter": {"name": "x"}, "set": {"v": 1}})

        # heartbeat_state allows only read/upsert, so a plain CAS is denied.
        assert response.status_code == 403
        api_module.db_manager.mongodb_adapter.find_one_and_update.assert_not_called()


@pytest.fixture
def mongo_adapter():
    adapter = MongoDBAdapter("mongodb://localhost:27017/test_db")
    adapter.client = MagicMock()
    adapter.db = MagicMock()
    adapter._connected = True
    coll = MagicMock()
    coll.find_one_and_update = AsyncMock(
        return_value={"_id": "oid", "service": "te", "version": 4}
    )
    adapter.db.__getitem__ = MagicMock(return_value=coll)
    return adapter


def _coll(adapter):
    return adapter.db.__getitem__.return_value


class TestAdapter:
    @pytest.mark.asyncio
    async def test_single_find_one_and_update_returning_the_new_document(
        self, mongo_adapter
    ):
        document = await mongo_adapter.find_one_and_update(
            "heartbeat_state", {"service": "te", "version": 3}, {"version": 4}
        )

        assert document == {"service": "te", "version": 4}
        _coll(mongo_adapter).find_one_and_update.assert_awaited_once_with(
            {"service": "te", "version": 3},
            {"$set": {"version": 4}},
            upsert=False,
            return_document=ReturnDocument.AFTER,
        )

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, mongo_adapter):
        _coll(mongo_adapter).find_one_and_update.return_value = None

        assert await mongo_adapter.find_one_and_update("c", {"k": 1}, {"v": 2}) is None

    @pytest.mark.asyncio
    async def test_upsert_duplicate_key_is_a_cas_conflict(self, mongo_adapter):
        """Same unique key, different expected values: not matched, not an error."""
        _coll(mongo_adapter).find_one_and_update.side_effect = DuplicateKeyError(
            "E11000"
        )

        document = await mongo_adapter.find_one_and_update(
            "heartbeat_state", {"service": "te", "version": 3}, {"v": 4}, upsert=True
        )

        assert document is None

    @pytest.mark.asyncio
    async def test_duplicate_key_without_upsert_is_an_error(self, mongo_adapter):
        _coll(mongo_adapter).find_one_and_update.side_effect = DuplicateKeyError(
            "E11000"
        )

        with pytest.raises(DatabaseError, match="compare-and-set"):
            await mongo_adapter.find_one_and_update("c", {"k": 1}, {"u": 2})

    @pytest.mark.asyncio
    async def test_driver_error_is_wrapped(self, mongo_adapter):
        _coll(mongo_adapter).find_one_and_update.side_effect = PyMongoError("down")

        with pytest.raises(DatabaseError, match="compare-and-set"):
            await mongo_adapter.find_one_and_update("c", {"k": 1}, {"v": 2})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filter_dict", "match"),
        [({}, "empty filter"), ({"$expr": 1}, "flat equality")],
    )
    async def test_refuses_unsafe_filters(self, mongo_adapter, filter_dict, match):
        with pytest.raises(DatabaseError, match=match):
            await mongo_adapter.find_one_and_update("c", filter_dict, {"v": 1})
        _coll(mongo_adapter).find_one_and_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_when_not_connected(self, mongo_adapter):
        mongo_adapter._connected = False

        with pytest.raises(DatabaseError, match="Not connected"):
            await mongo_adapter.find_one_and_update("c", {"k": 1}, {"v": 1})
