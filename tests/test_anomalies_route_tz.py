from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import data_manager.api.app as api_module
from data_manager.api.routes.anomalies import get_anomalies


def _logs() -> list[dict]:
    """Audit logs as the tz-aware Mongo client returns them."""
    return [
        {
            "details": "anomaly: spike",
            "severity": "high",
            "timestamp": datetime(2026, 10, day, tzinfo=UTC),
        }
        for day in (1, 3, 5)
    ] + [{"details": "outlier without a timestamp", "severity": "low"}]


async def _call(monkeypatch, **filters):
    class AuditRepository:
        def __init__(self, *args):
            pass

        async def get_recent_logs(self, dataset_id=None, limit=100):
            return _logs()

    monkeypatch.setattr("data_manager.db.repositories.AuditRepository", AuditRepository)
    monkeypatch.setattr(
        api_module,
        "db_manager",
        SimpleNamespace(mysql_adapter=object(), mongodb_adapter=object()),
        raising=False,
    )
    params = {
        "pair": "BTCUSDT",
        "severity": None,
        "status": None,
        "from_time": None,
        "to_time": None,
        "limit": 100,
        "offset": 0,
        "sort_by": "timestamp",
        "sort_order": "asc",
    }
    params.update(filters)
    return await get_anomalies(**params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bounds",
    [
        {"from_time": datetime(2026, 10, 2), "to_time": datetime(2026, 10, 4)},
        {
            "from_time": datetime(2026, 10, 2, tzinfo=UTC),
            "to_time": datetime(2026, 10, 4, tzinfo=UTC),
        },
    ],
)
async def test_naive_and_aware_bounds_filter_aware_mongo_timestamps(
    monkeypatch, bounds
):
    result = await _call(monkeypatch, **bounds)

    assert [a["timestamp"].day for a in result["data"]] == [3]


@pytest.mark.asyncio
async def test_sorting_aware_timestamps_tolerates_a_missing_one(monkeypatch):
    result = await _call(monkeypatch, sort_order="asc")

    assert "timestamp" not in result["data"][0]
    assert [a["timestamp"].day for a in result["data"][1:]] == [1, 3, 5]
