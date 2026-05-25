"""
Unit tests for data_manager.leader_election.LeaderElectionManager.

MongoDB client is mocked end-to-end (motor-style async ops). Tests cover the
real election logic: becoming leader on empty state, losing to active leader,
taking over stale leader, heartbeat send/verify, graceful release.
"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_manager.leader_election import LeaderElectionManager


def make_mongo_client():
    """Build a motor-style mock with .admin.command and __getitem__ for db access."""
    client = MagicMock()
    client.admin.command = AsyncMock(return_value={"ok": 1})

    db = MagicMock()
    leader_collection = MagicMock()
    leader_collection.create_index = AsyncMock()
    leader_collection.find_one = AsyncMock(return_value=None)
    leader_collection.update_one = AsyncMock()
    leader_collection.delete_one = AsyncMock()
    db.leader_election = leader_collection

    client.__getitem__ = MagicMock(return_value=db)
    return client, db, leader_collection


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_connects_and_ensures_indexes(self):
        client, db, coll = make_mongo_client()
        mgr = LeaderElectionManager()
        await mgr.initialize(client)
        assert mgr.mongodb_client is client
        assert mgr.mongodb_db is db
        # admin.command("ping") called
        client.admin.command.assert_called_once_with("ping")
        # Three indexes created on leader_election collection.
        assert coll.create_index.call_count == 3

    @pytest.mark.asyncio
    async def test_initialize_propagates_connect_error(self):
        client = MagicMock()
        client.admin.command = AsyncMock(side_effect=RuntimeError("mongo down"))
        client.__getitem__ = MagicMock(return_value=MagicMock())
        mgr = LeaderElectionManager()
        with pytest.raises(RuntimeError, match="mongo down"):
            await mgr.initialize(client)
        # State should be reset on failure.
        assert mgr.mongodb_client is None
        assert mgr.mongodb_db is None


class TestEnsureIndexes:
    @pytest.mark.asyncio
    async def test_returns_early_when_db_is_none(self):
        mgr = LeaderElectionManager()
        mgr.mongodb_db = None
        # Should not raise.
        await mgr._ensure_indexes()
        # No db access happened — nothing to assert other than reaching here.
        assert mgr.mongodb_db is None

    @pytest.mark.asyncio
    async def test_swallows_index_creation_errors(self):
        mgr = LeaderElectionManager()
        db = MagicMock()
        db.leader_election.create_index = AsyncMock(side_effect=RuntimeError("x"))
        mgr.mongodb_db = db
        # Must not raise.
        await mgr._ensure_indexes()
        # First call happened before error swallowed.
        db.leader_election.create_index.assert_called()


class TestStart:
    @pytest.mark.asyncio
    async def test_returns_false_when_db_not_initialized(self):
        mgr = LeaderElectionManager()
        # mongodb_db is None by default.
        assert await mgr.start() is False

    @pytest.mark.asyncio
    async def test_starts_as_leader_when_election_succeeds(self):
        client, db, coll = make_mongo_client()
        # find_one called twice: once in _try_become_leader before, once after verify.
        coll.find_one = AsyncMock(
            side_effect=[None, {"pod_id": "test-pod", "status": "leader"}]
        )
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        ok = await mgr.start()
        assert ok is True
        assert mgr.is_leader is True
        # Stop to clean up the heartbeat task.
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_starts_as_follower_when_leader_exists(self):
        client, db, coll = make_mongo_client()
        # Active leader (recent heartbeat).
        coll.find_one = AsyncMock(
            return_value={
                "pod_id": "other-pod",
                "last_heartbeat": datetime.now(UTC),
            }
        )
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        ok = await mgr.start()
        assert ok is True
        assert mgr.is_leader is False
        assert mgr.leader_pod_id == "other-pod"


class TestTryBecomeLeader:
    @pytest.mark.asyncio
    async def test_returns_false_when_db_is_none(self):
        mgr = LeaderElectionManager()
        # mongodb_db None.
        assert await mgr._try_become_leader() is False

    @pytest.mark.asyncio
    async def test_takes_over_stale_leader(self):
        client, db, coll = make_mongo_client()
        # Stale heartbeat (old leader).
        stale_leader = {
            "pod_id": "stale-pod",
            "last_heartbeat": datetime.now(UTC) - timedelta(hours=1),
        }
        new_leader = {"pod_id": "test-pod", "status": "leader"}
        coll.find_one = AsyncMock(side_effect=[stale_leader, new_leader])
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        ok = await mgr._try_become_leader()
        assert ok is True
        assert mgr.is_leader is True

    @pytest.mark.asyncio
    async def test_loses_election_when_other_pod_wins(self):
        client, db, coll = make_mongo_client()
        # No current leader, but verify returns a different pod (race lost).
        coll.find_one = AsyncMock(
            side_effect=[None, {"pod_id": "other-pod", "status": "leader"}]
        )
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        ok = await mgr._try_become_leader()
        assert ok is False
        assert mgr.is_leader is False
        assert mgr.leader_pod_id == "other-pod"

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        client, db, coll = make_mongo_client()
        coll.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        assert await mgr._try_become_leader() is False
        assert mgr.is_leader is False


class TestSendHeartbeat:
    @pytest.mark.asyncio
    async def test_returns_false_when_db_is_none(self):
        mgr = LeaderElectionManager()
        assert await mgr._send_heartbeat() is False

    @pytest.mark.asyncio
    async def test_returns_true_when_document_modified(self):
        client, db, coll = make_mongo_client()
        result = MagicMock()
        result.modified_count = 1
        coll.update_one = AsyncMock(return_value=result)
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        assert await mgr._send_heartbeat() is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_document_modified(self):
        client, db, coll = make_mongo_client()
        result = MagicMock()
        result.modified_count = 0
        coll.update_one = AsyncMock(return_value=result)
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        assert await mgr._send_heartbeat() is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        client, db, coll = make_mongo_client()
        coll.update_one = AsyncMock(side_effect=RuntimeError("x"))
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        assert await mgr._send_heartbeat() is False


class TestVerifyLeadership:
    @pytest.mark.asyncio
    async def test_returns_false_when_db_is_none(self):
        mgr = LeaderElectionManager()
        assert await mgr._verify_leadership() is False

    @pytest.mark.asyncio
    async def test_returns_true_when_pod_is_leader(self):
        client, db, coll = make_mongo_client()
        coll.find_one = AsyncMock(return_value={"pod_id": "test-pod"})
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        assert await mgr._verify_leadership() is True

    @pytest.mark.asyncio
    async def test_returns_false_when_pod_is_not_leader(self):
        client, db, coll = make_mongo_client()
        coll.find_one = AsyncMock(return_value={"pod_id": "other-pod"})
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.mongodb_db = db
        assert await mgr._verify_leadership() is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        client, db, coll = make_mongo_client()
        coll.find_one = AsyncMock(side_effect=RuntimeError("x"))
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        assert await mgr._verify_leadership() is False


class TestReleaseLeadership:
    @pytest.mark.asyncio
    async def test_returns_early_when_db_is_none(self):
        mgr = LeaderElectionManager()
        mgr.is_leader = True
        await mgr._release_leadership()
        # No DB ops should have happened, no exception.
        assert mgr.is_leader is True  # state untouched when db absent

    @pytest.mark.asyncio
    async def test_clears_is_leader_when_deleted(self):
        client, db, coll = make_mongo_client()
        result = MagicMock()
        result.deleted_count = 1
        coll.delete_one = AsyncMock(return_value=result)
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        mgr.is_leader = True
        await mgr._release_leadership()
        assert mgr.is_leader is False

    @pytest.mark.asyncio
    async def test_handles_no_document_deleted(self):
        client, db, coll = make_mongo_client()
        result = MagicMock()
        result.deleted_count = 0
        coll.delete_one = AsyncMock(return_value=result)
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        mgr.is_leader = True
        await mgr._release_leadership()
        # Still flips is_leader off.
        assert mgr.is_leader is False

    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        client, db, coll = make_mongo_client()
        coll.delete_one = AsyncMock(side_effect=RuntimeError("x"))
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        mgr.is_leader = True
        # Must not raise.
        await mgr._release_leadership()


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        mgr = LeaderElectionManager()
        # Should not raise even if never started.
        await mgr.stop()
        assert mgr._running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_heartbeat_task(self):
        client, db, coll = make_mongo_client()
        coll.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        mgr.is_leader = True

        async def long_running():
            await asyncio.sleep(60)

        mgr.heartbeat_task = asyncio.create_task(long_running())
        await mgr.stop()
        assert mgr.heartbeat_task.cancelled() or mgr.heartbeat_task.done()
        # Leadership was released.
        coll.delete_one.assert_called_once()


class TestGetStatus:
    def test_returns_status_dict(self):
        mgr = LeaderElectionManager()
        mgr.pod_id = "test-pod"
        mgr.is_leader = True
        mgr.leader_pod_id = "test-pod"
        status = mgr.get_status()
        assert isinstance(status, dict)
        assert status["pod_id"] == "test-pod"
        assert status["is_leader"] is True


class TestHeartbeatTimezoneHandling:
    """Regression tests for #169: MongoDB naive datetimes must not cause TypeError."""

    @pytest.mark.asyncio
    async def test_naive_heartbeat_treated_as_utc(self):
        client, db, coll = make_mongo_client()
        naive_hb = datetime.now() - timedelta(seconds=5)
        coll.find_one = AsyncMock(
            return_value={
                "pod_id": "other-pod",
                "last_heartbeat": naive_hb,
                "status": "leader",
            }
        )
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        mgr.election_timeout = 30
        result = await mgr._try_become_leader()
        assert result is False

    @pytest.mark.asyncio
    async def test_aware_heartbeat_unchanged(self):
        client, db, coll = make_mongo_client()
        aware_hb = datetime.now(UTC) - timedelta(seconds=5)
        coll.find_one = AsyncMock(
            return_value={
                "pod_id": "other-pod",
                "last_heartbeat": aware_hb,
                "status": "leader",
            }
        )
        mgr = LeaderElectionManager()
        mgr.mongodb_db = db
        mgr.election_timeout = 30
        result = await mgr._try_become_leader()
        assert result is False
