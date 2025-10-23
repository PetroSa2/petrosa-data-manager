"""
Pydantic models for schema registry operations.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from enum import Enum

from pydantic import BaseModel, Field, validator


class SchemaStatus(str, Enum):
    """Schema status enumeration."""
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    DELETED = "DELETED"


class CompatibilityMode(str, Enum):
    """Schema compatibility mode enumeration."""
    BACKWARD = "BACKWARD"  # New schema can read data written with old schema
    FORWARD = "FORWARD"    # Old schema can read data written with new schema
    FULL = "FULL"         # Both backward and forward compatible
    NONE = "NONE"         # No compatibility requirements


class SchemaDefinition(BaseModel):
    """Base schema definition model."""
    
    name: str = Field(..., description="Schema name", min_length=1, max_length=100)
    version: int = Field(..., description="Schema version", ge=1)
    schema: Dict[str, Any] = Field(..., description="JSON Schema definition")
    compatibility_mode: CompatibilityMode = Field(
        default=CompatibilityMode.BACKWARD,
        description="Schema compatibility mode"
    )
    status: SchemaStatus = Field(default=SchemaStatus.ACTIVE, description="Schema status")
    description: Optional[str] = Field(None, description="Schema description", max_length=500)
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Creation timestamp")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Last update timestamp")
    created_by: Optional[str] = Field(None, description="Creator identifier", max_length=100)
    
    @validator('name')
    def validate_name(cls, v):
        """Validate schema name format."""
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError("Schema name must contain only alphanumeric characters, underscores, and hyphens")
        return v.lower()
    
    @validator('schema')
    def validate_schema_json(cls, v):
        """Validate that schema is valid JSON Schema."""
        if not isinstance(v, dict):
            raise ValueError("Schema must be a JSON object")
        
        # Basic JSON Schema validation
        if 'type' not in v:
            raise ValueError("Schema must have a 'type' field")
        
        return v


class SchemaVersion(BaseModel):
    """Schema version information."""
    
    version: int = Field(..., description="Version number", ge=1)
    schema: Dict[str, Any] = Field(..., description="JSON Schema definition")
    compatibility_mode: CompatibilityMode = Field(..., description="Compatibility mode")
    status: SchemaStatus = Field(..., description="Version status")
    description: Optional[str] = Field(None, description="Version description")
    created_at: datetime = Field(..., description="Version creation timestamp")
    created_by: Optional[str] = Field(None, description="Version creator")


class SchemaRegistration(BaseModel):
    """Schema registration request model."""
    
    version: int = Field(..., description="Schema version", ge=1)
    schema: Dict[str, Any] = Field(..., description="JSON Schema definition")
    compatibility_mode: Optional[CompatibilityMode] = Field(
        default=None,
        description="Schema compatibility mode"
    )
    description: Optional[str] = Field(None, description="Schema description", max_length=500)
    created_by: Optional[str] = Field(None, description="Creator identifier", max_length=100)
    
    @validator('schema')
    def validate_schema_json(cls, v):
        """Validate that schema is valid JSON Schema."""
        if not isinstance(v, dict):
            raise ValueError("Schema must be a JSON object")
        
        if 'type' not in v:
            raise ValueError("Schema must have a 'type' field")
        
        return v


class SchemaUpdate(BaseModel):
    """Schema update request model."""
    
    schema: Optional[Dict[str, Any]] = Field(None, description="Updated JSON Schema definition")
    compatibility_mode: Optional[CompatibilityMode] = Field(None, description="Updated compatibility mode")
    status: Optional[SchemaStatus] = Field(None, description="Updated status")
    description: Optional[str] = Field(None, description="Updated description", max_length=500)
    
    @validator('schema')
    def validate_schema_json(cls, v):
        """Validate that schema is valid JSON Schema."""
        if v is not None:
            if not isinstance(v, dict):
                raise ValueError("Schema must be a JSON object")
            
            if 'type' not in v:
                raise ValueError("Schema must have a 'type' field")
        
        return v


class SchemaValidationRequest(BaseModel):
    """Schema validation request model."""
    
    database: str = Field(..., description="Target database", pattern="^(mysql|mongodb)$")
    schema_name: str = Field(..., description="Schema name", min_length=1, max_length=100)
    schema_version: Optional[int] = Field(None, description="Specific schema version", ge=1)
    data: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(..., description="Data to validate")
    
    @validator('schema_name')
    def validate_schema_name(cls, v):
        """Validate schema name format."""
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError("Schema name must contain only alphanumeric characters, underscores, and hyphens")
        return v.lower()


class SchemaValidationResponse(BaseModel):
    """Schema validation response model."""
    
    valid: bool = Field(..., description="Whether data is valid")
    errors: List[str] = Field(default_factory=list, description="Validation errors")
    warnings: List[str] = Field(default_factory=list, description="Validation warnings")
    schema_used: str = Field(..., description="Schema name and version used")
    validated_count: int = Field(..., description="Number of items validated")
    validation_time_ms: float = Field(..., description="Validation time in milliseconds")


class SchemaListResponse(BaseModel):
    """Schema list response model."""
    
    schemas: List[Dict[str, Any]] = Field(..., description="List of schemas")
    total_count: int = Field(..., description="Total number of schemas")
    database: Optional[str] = Field(None, description="Database filter applied")
    page: int = Field(default=1, description="Current page number")
    page_size: int = Field(default=100, description="Page size")
    has_next: bool = Field(..., description="Whether there are more pages")


class SchemaVersionListResponse(BaseModel):
    """Schema version list response model."""
    
    schema_name: str = Field(..., description="Schema name")
    versions: List[SchemaVersion] = Field(..., description="List of versions")
    total_versions: int = Field(..., description="Total number of versions")
    latest_version: int = Field(..., description="Latest version number")


class SchemaCompatibilityRequest(BaseModel):
    """Schema compatibility check request model."""
    
    database: str = Field(..., description="Target database", pattern="^(mysql|mongodb)$")
    schema_name: str = Field(..., description="Schema name", min_length=1, max_length=100)
    old_version: int = Field(..., description="Old schema version", ge=1)
    new_version: int = Field(..., description="New schema version", ge=1)


class SchemaCompatibilityResponse(BaseModel):
    """Schema compatibility check response model."""
    
    compatible: bool = Field(..., description="Whether schemas are compatible")
    compatibility_mode: str = Field(..., description="Compatibility mode used")
    breaking_changes: List[str] = Field(default_factory=list, description="Breaking changes found")
    warnings: List[str] = Field(default_factory=list, description="Compatibility warnings")
    migration_suggestions: List[str] = Field(default_factory=list, description="Migration suggestions")


class SchemaSearchRequest(BaseModel):
    """Schema search request model."""
    
    database: Optional[str] = Field(None, description="Database filter", pattern="^(mysql|mongodb)$")
    name_pattern: Optional[str] = Field(None, description="Name pattern to search", max_length=100)
    status: Optional[SchemaStatus] = Field(None, description="Status filter")
    created_after: Optional[datetime] = Field(None, description="Created after timestamp")
    created_before: Optional[datetime] = Field(None, description="Created before timestamp")
    page: int = Field(default=1, description="Page number", ge=1)
    page_size: int = Field(default=100, description="Page size", ge=1, le=1000)


class SchemaBootstrapRequest(BaseModel):
    """Schema bootstrap request model."""
    
    database: str = Field(..., description="Target database", pattern="^(mysql|mongodb)$")
    schemas: List[SchemaRegistration] = Field(..., description="Schemas to bootstrap", min_items=1)
    overwrite_existing: bool = Field(default=False, description="Whether to overwrite existing schemas")


class SchemaBootstrapResponse(BaseModel):
    """Schema bootstrap response model."""
    
    success: bool = Field(..., description="Whether bootstrap was successful")
    registered_count: int = Field(..., description="Number of schemas registered")
    skipped_count: int = Field(..., description="Number of schemas skipped")
    errors: List[str] = Field(default_factory=list, description="Bootstrap errors")
    registered_schemas: List[str] = Field(default_factory=list, description="Names of registered schemas")
