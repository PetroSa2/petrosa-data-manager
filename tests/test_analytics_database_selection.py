from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from data_manager.api.routes import analysis


class MotorLikeDatabase:
    def __init__(self, collections=None):
        self.collections = collections or {}

    def __bool__(self):
        raise NotImplementedError("database truth testing is not supported")

    def __getitem__(self, name):
        return self.collections[name]


def test_analytics_database_uses_configured_database_without_truth_testing():
    configured = MotorLikeDatabase()
    mongodb = SimpleNamespace(analytics_db=configured, db=object())

    assert analysis._analytics_database(mongodb) is configured


def test_analytics_database_falls_back_only_when_missing():
    primary = object()
    mongodb = SimpleNamespace(analytics_db=None, db=primary)

    assert analysis._analytics_database(mongodb) is primary


@pytest.mark.asyncio
async def test_closed_rounds_returns_explicit_error_when_execution_read_fails():
    class Collection:
        def find(self, _query):
            raise RuntimeError("analytics read failed")

    mongodb = SimpleNamespace(
        analytics_db=MotorLikeDatabase({"execution_events": Collection()}),
        db=object(),
    )
    manager = SimpleNamespace(mongodb_adapter=mongodb)

    with pytest.raises(HTTPException) as error:
        await analysis.compute_closed_rounds(manager)

    assert error.value.status_code == 500
    assert error.value.detail == "analytics read failed"
