"""MongoDB repository for per-service runtime configuration and its audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError


class ServiceConfigVersionConflict(Exception):
    """Raised when an optimistic version check does not match."""

    def __init__(self, current_version: int | None):
        self.current_version = current_version
        super().__init__("version conflict")


class ServiceConfigRepository:
    """Store service configuration in the database owned by the adapter."""

    def __init__(self, mongodb_adapter: Any):
        self.db = mongodb_adapter.db
        self.configs = self.db["service_configs"]
        self.audit = self.db["service_config_audit"]

    async def ensure_indexes(self) -> None:
        await self.configs.create_index(
            [("service", ASCENDING), ("key", ASCENDING)], unique=True
        )
        await self.audit.create_index(
            [("service", ASCENDING), ("key", ASCENDING), ("changed_at", DESCENDING)]
        )

    async def list(self, service: str) -> list[dict[str, Any]]:
        return [document async for document in self.configs.find({"service": service})]

    async def get(self, service: str, key: str) -> dict[str, Any] | None:
        return await self.configs.find_one({"service": service, "key": key})

    async def put(
        self,
        service: str,
        key: str,
        value: Any,
        changed_by: str,
        reason: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        old = await self.get(service, key)
        now = datetime.now(UTC)
        update = {
            "$set": {
                "value": value,
                "changed_by": changed_by,
                "reason": reason,
                "updated_at": now,
            },
            "$inc": {"version": 1},
        }
        if expected_version == 0:
            try:
                await self.configs.insert_one(
                    {
                        "service": service,
                        "key": key,
                        "value": value,
                        "version": 1,
                        "changed_by": changed_by,
                        "reason": reason,
                        "updated_at": now,
                    }
                )
            except DuplicateKeyError as exc:
                raise ServiceConfigVersionConflict(
                    (await self.get(service, key) or {}).get("version")
                ) from exc
            result = await self.get(service, key)
        else:
            query = {"service": service, "key": key}
            if expected_version is not None:
                query["version"] = expected_version
            result = await self.configs.find_one_and_update(
                query,
                update,
                upsert=expected_version is None,
                return_document=ReturnDocument.AFTER,
            )
            if result is None:
                current = await self.get(service, key)
                raise ServiceConfigVersionConflict(
                    current.get("version") if current else None
                )

        if result is None:
            raise RuntimeError("configuration write returned no document")
        await self.audit.insert_one(
            {
                "service": service,
                "key": key,
                "old_value": old.get("value") if old else None,
                "new_value": value,
                "version": result["version"],
                "changed_by": changed_by,
                "reason": reason,
                "changed_at": now,
            }
        )
        return result

    async def audit_entries(
        self, service: str, key: str | None, limit: int
    ) -> list[dict[str, Any]]:
        query = {"service": service}
        if key is not None:
            query["key"] = key
        cursor = self.audit.find(query).sort("changed_at", DESCENDING).limit(limit)
        return [document async for document in cursor]
