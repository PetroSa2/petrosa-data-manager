"""
Data catalog and metadata models.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SchemaField(BaseModel):
    """Schema field definition."""

    name: str = Field(..., description="Field name")
    type: str = Field(..., description="Field type")
    description: str = Field(..., description="Field description")
    required: bool = Field(..., description="Whether field is required")
    nullable: bool = Field(default=False, description="Whether field can be null")

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})


class SchemaDefinition(BaseModel):
    """Complete schema definition for a dataset."""

    schema_id: str = Field(..., description="Schema identifier")
    version: str = Field(..., description="Schema version")
    fields: list[SchemaField] = Field(..., description="Schema fields")
    primary_keys: list[str] = Field(..., description="Primary key fields")
    indexes: list[str] = Field(default_factory=list, description="Indexed fields")
    created_at: datetime = Field(..., description="Schema creation timestamp")
    updated_at: datetime = Field(..., description="Schema last update timestamp")

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})


class DatasetMetadata(BaseModel):
    """Metadata for a dataset."""

    dataset_id: str = Field(..., description="Dataset identifier")
    name: str = Field(..., description="Dataset name")
    description: str = Field(..., description="Dataset description")
    category: str = Field(
        ..., description="Dataset category (e.g., 'market_data', 'analytics')"
    )
    schema_id: str = Field(..., description="Schema identifier")
    storage_type: str = Field(..., description="Storage type (postgresql/mongodb)")
    table_name: str | None = Field(None, description="Table/collection name")
    owner: str = Field(..., description="Dataset owner/maintainer")
    update_frequency: str = Field(..., description="Expected update frequency")
    retention_days: int | None = Field(
        None, description="Data retention period in days"
    )
    tags: list[str] = Field(default_factory=list, description="Dataset tags")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )
    created_at: datetime = Field(..., description="Dataset creation timestamp")
    updated_at: datetime = Field(..., description="Dataset last update timestamp")

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})


class LineageRecord(BaseModel):
    """Data lineage tracking record."""

    lineage_id: str = Field(..., description="Lineage record identifier")
    dataset_id: str = Field(..., description="Dataset identifier")
    source_dataset_id: str | None = Field(None, description="Source dataset identifier")
    transformation: str = Field(..., description="Transformation applied")
    transformation_params: dict[str, Any] = Field(
        default_factory=dict, description="Transformation parameters"
    )
    input_records: int = Field(..., description="Number of input records")
    output_records: int = Field(..., description="Number of output records")
    execution_time_seconds: float = Field(..., description="Execution time in seconds")
    status: str = Field(..., description="Execution status (success/failed)")
    error_message: str | None = Field(None, description="Error message if failed")
    timestamp: datetime = Field(..., description="Lineage record timestamp")

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})


class DatasetCatalog(BaseModel):
    """Complete catalog of all datasets."""

    datasets: list[DatasetMetadata] = Field(..., description="List of all datasets")
    schemas: list[SchemaDefinition] = Field(..., description="List of all schemas")
    total_count: int = Field(..., description="Total number of datasets")
    last_updated: datetime = Field(..., description="Catalog last update timestamp")

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})
