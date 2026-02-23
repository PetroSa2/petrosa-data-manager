"""
Pydantic models for configuration and audit trails.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ConfigAudit(BaseModel):
    """Configuration audit record."""

    id: Optional[str] = Field(None, alias="_id")
    config_type: str = Field(..., description="Type of config: 'application' or 'strategy'")
    strategy_id: Optional[str] = Field(None, description="Strategy identifier")
    symbol: Optional[str] = Field(None, description="Trading symbol")
    action: str = Field(..., description="Action: 'CREATE', 'UPDATE', 'DELETE', 'ROLLBACK'")
    old_parameters: Optional[dict[str, Any]] = Field(None, description="Previous values")
    new_parameters: Optional[dict[str, Any]] = Field(None, description="New values")
    changed_by: str = Field(..., description="Who made the change")
    changed_at: datetime = Field(default_factory=datetime.utcnow)
    reason: Optional[str] = Field(None, description="Reason for change")
    version: int = Field(..., description="Configuration version after change")


class ConfigurationDocument(BaseModel):
    """Generic configuration document for storage."""

    config_type: str
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    parameters: dict[str, Any]
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    changed_by: str
    reason: Optional[str] = None
