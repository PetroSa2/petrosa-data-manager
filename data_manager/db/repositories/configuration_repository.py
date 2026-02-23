"""
Repository for configuration management and audit trails.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from data_manager.db.repositories.base_repository import BaseRepository
from data_manager.models.config import ConfigAudit, ConfigurationDocument

logger = logging.getLogger(__name__)


class ConfigurationRepository(BaseRepository):
    """
    Repository for managing application and strategy configurations in MongoDB.
    
    Provides auditing and rollback capabilities.
    """

    async def get_app_config(self) -> Optional[dict[str, Any]]:
        """Get the current application configuration."""
        if not self.mongodb or not self.mongodb.is_connected:
            return None
            
        try:
            # Application config is a single document in app_config collection
            config = await self.mongodb.db.app_config.find_one({})
            if config:
                config.pop("_id", None)
            return config
        except Exception as e:
            logger.error(f"Error fetching app config: {e}")
            return None

    async def upsert_app_config(
        self, 
        parameters: dict[str, Any], 
        changed_by: str, 
        reason: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """
        Update application configuration and create audit record.
        """
        if not self.mongodb or not self.mongodb.is_connected:
            return None

        try:
            now = datetime.utcnow()
            existing = await self.get_app_config()
            
            old_params = existing.get("parameters") if existing else None
            version = (existing.get("version", 0) + 1) if existing else 1
            created_at = existing.get("created_at", now) if existing else now

            config_doc = {
                "config_type": "application",
                "parameters": parameters,
                "version": version,
                "created_at": created_at,
                "updated_at": now,
                "changed_by": changed_by,
                "reason": reason
            }

            # Update current config
            await self.mongodb.db.app_config.replace_one({}, config_doc, upsert=True)

            # Create audit record
            audit_record = ConfigAudit(
                config_type="application",
                action="UPDATE" if existing else "CREATE",
                old_parameters=old_params,
                new_parameters=parameters,
                changed_by=changed_by,
                changed_at=now,
                reason=reason,
                version=version
            )
            
            await self.mongodb.db.app_config_audit.insert_one(audit_record.model_dump(by_alias=True))
            
            return config_doc

        except Exception as e:
            logger.error(f"Error upserting app config: {e}")
            return None

    async def get_strategy_config(
        self, 
        strategy_id: str, 
        symbol: Optional[str] = None,
        side: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Get strategy configuration (global, symbol, or symbol-side)."""
        if not self.mongodb or not self.mongodb.is_connected:
            return None

        try:
            query = {"strategy_id": strategy_id, "symbol": symbol, "side": side}
            collection = self.mongodb.db.strategy_configs
            
            config = await collection.find_one(query)
            if config:
                config.pop("_id", None)
            return config
        except Exception as e:
            logger.error(f"Error fetching strategy config for {strategy_id}: {e}")
            return None

    async def upsert_strategy_config(
        self,
        strategy_id: str,
        parameters: dict[str, Any],
        changed_by: str,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        reason: Optional[str] = None,
        action: str = "UPDATE"
    ) -> Optional[dict[str, Any]]:
        """Update strategy configuration and create audit record."""
        if not self.mongodb or not self.mongodb.is_connected:
            return None

        try:
            now = datetime.utcnow()
            existing = await self.get_strategy_config(strategy_id, symbol, side)
            
            old_params = existing.get("parameters") if existing else None
            version = (existing.get("version", 0) + 1) if existing else 1
            created_at = existing.get("created_at", now) if existing else now

            config_doc = {
                "config_type": "strategy",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": side,
                "parameters": parameters,
                "version": version,
                "created_at": created_at,
                "updated_at": now,
                "changed_by": changed_by,
                "reason": reason
            }

            # Update current config
            await self.mongodb.db.strategy_configs.replace_one(
                {"strategy_id": strategy_id, "symbol": symbol, "side": side},
                config_doc,
                upsert=True
            )

            # Create audit record
            audit_record = ConfigAudit(
                config_type="strategy",
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                action=action if existing else "CREATE",
                old_parameters=old_params,
                new_parameters=parameters,
                changed_by=changed_by,
                changed_at=now,
                reason=reason,
                version=version
            )
            
            await self.mongodb.db.strategy_config_audit.insert_one(audit_record.model_dump(by_alias=True))
            
            return config_doc

        except Exception as e:
            logger.error(f"Error upserting strategy config: {e}")
            return None

    async def get_audit_trail(
        self,
        config_type: str,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get audit trail for a configuration."""
        if not self.mongodb or not self.mongodb.is_connected:
            return []

        try:
            collection_name = "app_config_audit" if config_type == "application" else "strategy_config_audit"
            collection = self.mongodb.db[collection_name]
            
            query = {}
            if strategy_id:
                query["strategy_id"] = strategy_id
            if symbol:
                query["symbol"] = symbol
            if side:
                query["side"] = side
                
            cursor = collection.find(query).sort("changed_at", -1).limit(limit)
            records = await cursor.to_list(length=limit)
            
            for record in records:
                record["_id"] = str(record["_id"])
                if "changed_at" in record and isinstance(record["changed_at"], datetime):
                    record["changed_at"] = record["changed_at"].isoformat()
                    
            return records
        except Exception as e:
            logger.error(f"Error fetching audit trail: {e}")
            return []

    async def rollback(
        self,
        config_type: str,
        changed_by: str,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        target_version: Optional[int] = None,
        reason: Optional[str] = None
    ) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
        """Rollback configuration to a previous version."""
        if not self.mongodb or not self.mongodb.is_connected:
            return False, "Database not connected", None

        try:
            # 1. Find the target version parameters
            collection_name = "app_config_audit" if config_type == "application" else "strategy_config_audit"
            collection = self.mongodb.db[collection_name]
            
            query = {}
            if strategy_id:
                query["strategy_id"] = strategy_id
            if symbol:
                query["symbol"] = symbol
            if side:
                query["side"] = side
                
            if target_version:
                query["version"] = target_version
                target_record = await collection.find_one(query)
            else:
                # Get the previous version (skip the current one)
                cursor = collection.find(query).sort("changed_at", -1).skip(1).limit(1)
                records = await cursor.to_list(length=1)
                target_record = records[0] if records else None

            if not target_record:
                return False, "No previous version found to rollback to", None

            target_params = target_record.get("new_parameters")
            if not target_params:
                return False, "Target version has no parameters", None

            # 2. Apply the parameters as a new version
            rollback_reason = reason or f"Rollback to version {target_record.get('version')}"
            
            if config_type == "application":
                result = await self.upsert_app_config(
                    parameters=target_params,
                    changed_by=changed_by,
                    reason=rollback_reason
                )
            else:
                result = await self.upsert_strategy_config(
                    strategy_id=strategy_id,
                    parameters=target_params,
                    changed_by=changed_by,
                    symbol=symbol,
                    side=side,
                    reason=rollback_reason,
                    action="ROLLBACK"
                )

            if result:
                return True, None, result
            else:
                return False, "Failed to apply rollback parameters", None

        except Exception as e:
            logger.error(f"Error during rollback: {e}")
            return False, str(e), None
