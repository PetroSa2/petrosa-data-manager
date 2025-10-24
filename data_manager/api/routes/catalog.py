"""
Data catalog endpoints for dataset metadata and schemas.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel

import data_manager.api.app as api_module

logger = logging.getLogger(__name__)

router = APIRouter()


class DatasetInfo(BaseModel):
    """Dataset information."""

    dataset_id: str
    name: str
    description: str
    category: str
    owner: str
    update_frequency: str
    tags: list[str]


class DatasetListResponse(BaseModel):
    """Dataset list response."""

    datasets: list[DatasetInfo]
    total_count: int
    last_updated: datetime


class DatasetDetailResponse(BaseModel):
    """Detailed dataset information."""

    dataset_id: str
    name: str
    description: str
    category: str
    schema_id: str
    storage_type: str
    metadata: dict
    created_at: datetime
    updated_at: datetime


class SchemaResponse(BaseModel):
    """Schema definition response."""

    schema_id: str
    version: str
    fields: list[dict]
    primary_keys: list[str]
    created_at: datetime


class LineageResponse(BaseModel):
    """Data lineage response."""

    dataset_id: str
    lineage: list[dict]
    metadata: dict


@router.get("/datasets")
async def list_datasets(
    category: str | None = Query(None, description="Filter by category"),
    owner: str | None = Query(None, description="Filter by owner"),
    search: str | None = Query(None, description="Text search in name/description"),
    limit: int = Query(
        50,
        ge=1,
        le=500,
        description="Maximum number of results (default: 50, max: 500)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset (default: 0)"),
    sort_by: str = Query(
        "name", description="Sort by field (name, created_at, updated_at)"
    ),
    sort_order: str = Query("asc", description="Sort order (asc, desc)"),
) -> dict:
    """
    List all available datasets in the catalog with filtering and pagination.

    Returns summary information for each dataset with support for filtering
    by category, owner, and text search. Results are paginated and sortable.
    """
    # Get datasets from catalog repository
    if api_module.db_manager and api_module.db_manager.mysql_adapter:
        from data_manager.db.repositories import CatalogRepository

        catalog_repo = CatalogRepository(
            api_module.db_manager.mysql_adapter, api_module.db_manager.mongodb_adapter
        )
        datasets_data = catalog_repo.get_all_datasets()

        datasets = [
            DatasetInfo(
                dataset_id=d.get("dataset_id", ""),
                name=d.get("name", ""),
                description=d.get("description", ""),
                category=d.get("category", ""),
                owner=d.get("owner", ""),
                update_frequency=d.get("update_frequency", ""),
                tags=[],
            )
            for d in datasets_data
        ]

        # Apply filters
        if category:
            datasets = [d for d in datasets if d.category == category]

        if owner:
            datasets = [d for d in datasets if d.owner == owner]

        if search:
            search_lower = search.lower()
            datasets = [
                d
                for d in datasets
                if search_lower in d.name.lower()
                or search_lower in d.description.lower()
            ]

        total_count = len(datasets)

        # Apply sorting
        reverse = sort_order.lower() == "desc"
        if sort_by == "name":
            datasets.sort(key=lambda x: x.name, reverse=reverse)
        # Note: created_at and updated_at would need to be added to DatasetInfo model

        # Apply pagination
        paginated_datasets = datasets[offset : offset + limit]

        return {
            "data": paginated_datasets,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1,
                "pages": (total_count + limit - 1) // limit if limit > 0 else 0,
                "has_next": offset + limit < total_count,
                "has_previous": offset > 0,
            },
            "filters_applied": {
                "category": category,
                "owner": owner,
                "search": search,
            },
            "sort": {
                "by": sort_by,
                "order": sort_order,
            },
            "last_updated": datetime.utcnow().isoformat(),
        }

    return {
        "data": [],
        "pagination": {
            "total": 0,
            "limit": limit,
            "offset": offset,
            "page": 1,
            "pages": 0,
            "has_next": False,
            "has_previous": False,
        },
        "filters_applied": {
            "category": category,
            "owner": owner,
            "search": search,
        },
        "sort": {
            "by": sort_by,
            "order": sort_order,
        },
        "last_updated": datetime.utcnow().isoformat(),
    }


@router.get("/datasets/{dataset_id}")
async def get_dataset_metadata(
    dataset_id: str = Path(..., description="Dataset identifier"),
) -> DatasetDetailResponse:
    """
    Get detailed metadata for a specific dataset.

    Includes schema reference, storage information, and ownership.
    """
    # TODO: Implement actual dataset metadata retrieval
    return DatasetDetailResponse(
        dataset_id=dataset_id,
        name="Dataset Name",
        description="Dataset description",
        category="market_data",
        schema_id="schema_v1",
        storage_type="mongodb",
        metadata={},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


@router.get("/schemas/{dataset_id}")
async def get_schema(
    dataset_id: str = Path(..., description="Dataset identifier"),
) -> SchemaResponse:
    """
    Get schema definition for a dataset.

    Returns complete field definitions and constraints.
    """
    # TODO: Implement actual schema retrieval
    return SchemaResponse(
        schema_id=f"{dataset_id}_schema",
        version="1.0.0",
        fields=[],
        primary_keys=[],
        created_at=datetime.utcnow(),
    )


@router.get("/lineage/{dataset_id}")
async def get_lineage(
    dataset_id: str = Path(..., description="Dataset identifier"),
) -> LineageResponse:
    """
    Get data lineage information for a dataset.

    Returns transformation history and source tracking.
    """
    # TODO: Implement actual lineage retrieval
    return LineageResponse(
        dataset_id=dataset_id,
        lineage=[],
        metadata={
            "last_updated": datetime.utcnow().isoformat(),
        },
    )
