#!/usr/bin/env python3
"""
Setup MongoDB collections for leader election.

This script creates the necessary collections and indexes for the leader election
mechanism used by the Data Manager auditor and analytics schedulers.
"""

import asyncio
import logging
import sys
from datetime import datetime

import motor.motor_asyncio

# Add parent directory to path for imports
sys.path.insert(0, ".")

import constants  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def setup_leader_election_collections():
    """Create and configure MongoDB collections for leader election."""
    try:
        # Connect to MongoDB
        logger.info(f"Connecting to MongoDB: {constants.MONGODB_URL}")
        client = motor.motor_asyncio.AsyncIOMotorClient(constants.MONGODB_URL)
        db = client[constants.MONGODB_DB]

        # Test connection
        await client.admin.command("ping")
        logger.info("Successfully connected to MongoDB")

        # Create leader_election collection
        logger.info("Setting up leader_election collection...")

        # Check if collection exists
        collections = await db.list_collection_names()

        if "leader_election" not in collections:
            await db.create_collection("leader_election")
            logger.info("Created leader_election collection")
        else:
            logger.info("leader_election collection already exists")

        # Create indexes
        leader_collection = db.leader_election

        # Index on status for quick leader lookup
        await leader_collection.create_index("status")
        logger.info("Created index on 'status'")

        # Index on pod_id for pod-specific queries
        await leader_collection.create_index("pod_id")
        logger.info("Created index on 'pod_id'")

        # TTL index on last_heartbeat for automatic cleanup
        # Documents will be automatically deleted after timeout * 2 seconds
        ttl_seconds = constants.LEADER_ELECTION_TIMEOUT * 2
        await leader_collection.create_index(
            "last_heartbeat", expireAfterSeconds=ttl_seconds
        )
        logger.info(
            f"Created TTL index on 'last_heartbeat' (expireAfterSeconds={ttl_seconds})"
        )

        # Create distributed_locks collection
        logger.info("Setting up distributed_locks collection...")

        if "distributed_locks" not in collections:
            await db.create_collection("distributed_locks")
            logger.info("Created distributed_locks collection")
        else:
            logger.info("distributed_locks collection already exists")

        locks_collection = db.distributed_locks

        # Index on lock_name for quick lock lookup
        await locks_collection.create_index("lock_name", unique=True)
        logger.info("Created unique index on 'lock_name'")

        # Index on pod_id for pod-specific lock queries
        await locks_collection.create_index("pod_id")
        logger.info("Created index on 'pod_id'")

        # TTL index on expires_at for automatic lock cleanup
        await locks_collection.create_index("expires_at", expireAfterSeconds=0)
        logger.info("Created TTL index on 'expires_at'")

        # Verify indexes
        logger.info("\nVerifying indexes...")
        leader_indexes = await leader_collection.index_information()
        logger.info(f"leader_election indexes: {list(leader_indexes.keys())}")

        locks_indexes = await locks_collection.index_information()
        logger.info(f"distributed_locks indexes: {list(locks_indexes.keys())}")

        # Insert test document to verify permissions
        logger.info("\nTesting write permissions...")
        test_doc = {
            "status": "test",
            "pod_id": "setup_script",
            "elected_at": datetime.utcnow(),
            "last_heartbeat": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        result = await leader_collection.insert_one(test_doc)
        logger.info(f"Test document inserted with _id: {result.inserted_id}")

        # Clean up test document
        await leader_collection.delete_one({"_id": result.inserted_id})
        logger.info("Test document cleaned up")

        logger.info("\n✅ Leader election setup completed successfully!")

        # Close connection
        client.close()

    except Exception as e:
        logger.error(f"❌ Error setting up leader election: {e}", exc_info=True)
        sys.exit(1)


async def verify_setup():
    """Verify the leader election setup."""
    try:
        logger.info("\n" + "=" * 60)
        logger.info("Verifying leader election setup...")
        logger.info("=" * 60)

        client = motor.motor_asyncio.AsyncIOMotorClient(constants.MONGODB_URL)
        db = client[constants.MONGODB_DB]

        # Check collections
        collections = await db.list_collection_names()
        logger.info(f"\nCollections: {collections}")

        required_collections = ["leader_election", "distributed_locks"]
        for coll in required_collections:
            if coll in collections:
                logger.info(f"✅ {coll} collection exists")
            else:
                logger.error(f"❌ {coll} collection not found!")

        # Check indexes
        if "leader_election" in collections:
            leader_indexes = await db.leader_election.index_information()
            logger.info("\nleader_election indexes:")
            for idx_name, idx_info in leader_indexes.items():
                logger.info(f"  - {idx_name}: {idx_info}")

        if "distributed_locks" in collections:
            locks_indexes = await db.distributed_locks.index_information()
            logger.info("\ndistributed_locks indexes:")
            for idx_name, idx_info in locks_indexes.items():
                logger.info(f"  - {idx_name}: {idx_info}")

        client.close()
        logger.info("\n✅ Verification completed!")

    except Exception as e:
        logger.error(f"❌ Error verifying setup: {e}", exc_info=True)
        sys.exit(1)


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("MongoDB Leader Election Setup Script")
    logger.info("=" * 60)
    logger.info(f"MongoDB URL: {constants.MONGODB_URL}")
    logger.info(f"Database: {constants.MONGODB_DB}")
    logger.info(f"Leader election timeout: {constants.LEADER_ELECTION_TIMEOUT}s")
    logger.info(f"Heartbeat interval: {constants.LEADER_ELECTION_HEARTBEAT_INTERVAL}s")
    logger.info("=" * 60)

    # Setup collections
    await setup_leader_election_collections()

    # Verify setup
    await verify_setup()

    logger.info("\n" + "=" * 60)
    logger.info("Setup completed successfully!")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
