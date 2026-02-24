"""
Leader Election Manager for Data Manager using MongoDB.

This module provides distributed leader election capabilities to ensure only one
pod runs background schedulers (auditor, analytics) across multiple replicas.

Based on the pattern from petrosa-tradeengine/shared/distributed_lock.py
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Any

import constants

logger = logging.getLogger(__name__)


class LeaderElectionManager:
    """Manages leader election for coordination across pods using MongoDB."""

    def __init__(self) -> None:
        """Initialize leader election manager."""
        self.pod_id = os.getenv("HOSTNAME", str(uuid.uuid4()))
        self.heartbeat_interval = constants.LEADER_ELECTION_HEARTBEAT_INTERVAL
        self.election_timeout = constants.LEADER_ELECTION_TIMEOUT
        self.is_leader = False
        self.leader_pod_id: str | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.mongodb_client: Any = None
        self.mongodb_db: Any = None
        self._running = False

    async def initialize(self, mongodb_client: Any) -> None:
        """
        Initialize leader election manager with MongoDB connection.

        Args:
            mongodb_client: Motor MongoDB client instance
        """
        try:
            self.mongodb_client = mongodb_client

            # Get database from MongoDB connection string
            # Extract database name from MONGODB_URL or use default
            database_name = constants.MONGODB_DB
            self.mongodb_db = self.mongodb_client[database_name]

            # Test connection
            await self.mongodb_client.admin.command("ping")
            logger.info(f"Leader election initialized for pod {self.pod_id}")

            # Create indexes for leader election collection
            await self._ensure_indexes()

        except Exception as e:
            logger.error(f"Failed to initialize leader election: {e}", exc_info=True)
            self.mongodb_client = None
            self.mongodb_db = None
            raise

    async def _ensure_indexes(self) -> None:
        """Ensure required indexes exist on leader election collection."""
        try:
            if self.mongodb_db is None:
                return

            leader_collection = self.mongodb_db.leader_election

            # Index on status for quick leader lookup
            await leader_collection.create_index("status")

            # Index on pod_id for pod-specific queries
            await leader_collection.create_index("pod_id")

            # TTL index on last_heartbeat for automatic cleanup of stale entries
            await leader_collection.create_index(
                "last_heartbeat", expireAfterSeconds=self.election_timeout * 2
            )

            logger.debug("Leader election indexes ensured")

        except Exception as e:
            logger.warning(f"Failed to create leader election indexes: {e}")

    async def start(self) -> bool:
        """
        Start leader election process.

        Returns:
            True if successfully became leader or started as follower
        """
        if self.mongodb_db is None:
            logger.error("Cannot start leader election: MongoDB not initialized")
            return False

        try:
            self._running = True

            # Try to become leader
            await self._try_become_leader()

            if self.is_leader:
                logger.info(f"Pod {self.pod_id} elected as LEADER")
                # Start heartbeat task
                self.heartbeat_task = asyncio.create_task(self._maintain_leadership())
            else:
                logger.info(
                    f"Pod {self.pod_id} is a FOLLOWER. Leader: {self.leader_pod_id}"
                )

            return True

        except Exception as e:
            logger.error(f"Error starting leader election: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop leader election and release leadership if held."""
        self._running = False

        # Stop heartbeat task
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass

        # Release leadership if we are the leader
        if self.is_leader:
            await self._release_leadership()

        logger.info("Leader election stopped")

    async def _try_become_leader(self) -> bool:
        """
        Try to become the leader pod using MongoDB atomic operations.

        Returns:
            True if became leader, False otherwise
        """
        if self.mongodb_db is None:
            return False

        try:
            leader_election = self.mongodb_db.leader_election

            # Check if there's already a leader
            current_leader = await leader_election.find_one({"status": "leader"})

            if current_leader:
                current_leader_pod = current_leader["pod_id"]
                last_heartbeat = current_leader["last_heartbeat"]

                # Check if current leader is stale (no heartbeat for timeout period)
                if (
                    last_heartbeat
                    and (datetime.utcnow() - last_heartbeat).total_seconds()
                    < self.election_timeout
                ):
                    # Current leader is still active
                    self.is_leader = False
                    self.leader_pod_id = current_leader_pod
                    logger.debug(
                        f"Pod {self.pod_id} is a follower. "
                        f"Active leader: {current_leader_pod}"
                    )
                    return False

                logger.info(
                    f"Current leader {current_leader_pod} is stale. "
                    f"Attempting to take leadership..."
                )

            # Try to become leader using upsert
            _ = await leader_election.update_one(
                {"status": "leader"},
                {
                    "$set": {
                        "pod_id": self.pod_id,
                        "elected_at": datetime.utcnow(),
                        "last_heartbeat": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                },
                upsert=True,
            )

            # Verify we became the leader by reading back
            await asyncio.sleep(0.1)  # Small delay for consistency
            leader_doc = await leader_election.find_one({"status": "leader"})

            if leader_doc and leader_doc["pod_id"] == self.pod_id:
                self.is_leader = True
                self.leader_pod_id = self.pod_id
                logger.info(f"Pod {self.pod_id} successfully elected as leader")
                return True
            else:
                self.is_leader = False
                self.leader_pod_id = leader_doc["pod_id"] if leader_doc else None
                logger.info(
                    f"Pod {self.pod_id} lost leader election. "
                    f"Leader: {self.leader_pod_id}"
                )
                return False

        except Exception as e:
            logger.error(f"Error in leader election: {e}", exc_info=True)
            self.is_leader = False
            return False

    async def _maintain_leadership(self) -> None:
        """Maintain leadership by sending periodic heartbeats."""
        logger.info(f"Starting leadership heartbeat for pod {self.pod_id}")

        while self._running and self.is_leader:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                if not self._running:
                    break

                # Send heartbeat
                success = await self._send_heartbeat()

                if not success:
                    logger.warning("Failed to send heartbeat, checking leadership...")
                    # Verify we're still the leader
                    if not await self._verify_leadership():
                        logger.error(
                            f"Lost leadership! Pod {self.pod_id} is no longer leader"
                        )
                        self.is_leader = False
                        break

            except asyncio.CancelledError:
                logger.info("Heartbeat task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in heartbeat maintenance: {e}", exc_info=True)
                await asyncio.sleep(5)  # Backoff on error

        logger.info("Leadership heartbeat stopped")

    async def _send_heartbeat(self) -> bool:
        """
        Send heartbeat to update last_heartbeat timestamp.

        Returns:
            True if heartbeat successful
        """
        if self.mongodb_db is None:
            return False

        try:
            leader_election = self.mongodb_db.leader_election
            result = await leader_election.update_one(
                {"status": "leader", "pod_id": self.pod_id},
                {
                    "$set": {
                        "last_heartbeat": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )

            if result.modified_count > 0:
                logger.debug(f"Heartbeat sent by leader {self.pod_id}")
                return True
            else:
                logger.warning(
                    f"Heartbeat failed: No document modified for pod {self.pod_id}"
                )
                return False

        except Exception as e:
            logger.error(f"Error sending heartbeat: {e}")
            return False

    async def _verify_leadership(self) -> bool:
        """
        Verify that this pod is still the leader.

        Returns:
            True if still leader
        """
        if self.mongodb_db is None:
            return False

        try:
            leader_election = self.mongodb_db.leader_election
            leader_doc = await leader_election.find_one({"status": "leader"})

            return leader_doc and leader_doc["pod_id"] == self.pod_id

        except Exception as e:
            logger.error(f"Error verifying leadership: {e}")
            return False

    async def _release_leadership(self) -> None:
        """Release leadership gracefully."""
        if self.mongodb_db is None:
            return

        try:
            leader_election = self.mongodb_db.leader_election
            result = await leader_election.delete_one(
                {"status": "leader", "pod_id": self.pod_id}
            )

            if result.deleted_count > 0:
                logger.info(f"Leadership released by pod {self.pod_id}")
            else:
                logger.warning(f"No leadership to release for pod {self.pod_id}")

            self.is_leader = False

        except Exception as e:
            logger.error(f"Error releasing leadership: {e}")

    def get_status(self) -> dict:
        """
        Get current leader election status.

        Returns:
            Dictionary with status information
        """
        return {
            "pod_id": self.pod_id,
            "is_leader": self.is_leader,
            "leader_pod_id": self.leader_pod_id,
            "running": self._running,
            "heartbeat_interval": self.heartbeat_interval,
            "election_timeout": self.election_timeout,
        }
