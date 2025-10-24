"""
Data health and quality metrics models.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DataHealthMetrics(BaseModel):
    """Data health metrics for a dataset."""

    completeness: float = Field(
        ..., ge=0.0, le=100.0, description="Data completeness percentage"
    )
    freshness_seconds: int = Field(
        ..., description="Age of most recent data in seconds"
    )
    gaps_count: int = Field(..., ge=0, description="Number of gaps detected")
    duplicates_count: int = Field(..., ge=0, description="Number of duplicate records")
    consistency_score: float = Field(
        ..., ge=0.0, le=100.0, description="Data consistency score"
    )
    quality_score: float = Field(
        ..., ge=0.0, le=100.0, description="Overall quality score"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class GapInfo(BaseModel):
    """Information about a data gap."""

    start_time: datetime = Field(..., description="Gap start timestamp")
    end_time: datetime = Field(..., description="Gap end timestamp")
    duration_seconds: int = Field(..., description="Gap duration in seconds")
    expected_records: int = Field(..., description="Expected number of records")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class DatasetHealth(BaseModel):
    """Complete health status for a dataset."""

    dataset_id: str = Field(..., description="Dataset identifier")
    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str | None = Field(None, description="Timeframe for time series data")
    metrics: DataHealthMetrics = Field(..., description="Health metrics")
    gaps: list[GapInfo] = Field(
        default_factory=list, description="List of detected gaps"
    )
    last_audit: datetime = Field(..., description="Last audit timestamp")
    last_update: datetime = Field(..., description="Last data update timestamp")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class HealthSummary(BaseModel):
    """Summary of health across all datasets."""

    total_datasets: int = Field(..., description="Total number of datasets")
    healthy_datasets: int = Field(..., description="Number of healthy datasets")
    degraded_datasets: int = Field(..., description="Number of degraded datasets")
    unhealthy_datasets: int = Field(..., description="Number of unhealthy datasets")
    overall_score: float = Field(
        ..., ge=0.0, le=100.0, description="Overall health score"
    )
    timestamp: datetime = Field(..., description="Summary timestamp")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
