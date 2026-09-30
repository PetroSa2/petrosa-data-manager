from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from data_manager.maintenance import (
    index_usage_snapshot as ius,
    storage_inventory as si,
)


def test_storage_inventory_emits_report_as_one_structured_log_record(
    caplog, capsys, monkeypatch
):
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("MYSQL_URI", raising=False)

    rc = si.main(["--mongo-only", "--json"])

    assert rc == si.EXIT_MONGO_FAILED
    assert capsys.readouterr().out == ""
    record = next(r for r in caplog.records if r.name == si.__name__)
    payload = json.loads(record.getMessage().split(" ", 1)[1])
    assert payload["level"] == "INFO"
    assert payload["report"]["mongo_error"] == "MONGODB_URL not set"


def test_index_usage_json_is_logged_with_explicit_level(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    document = {"captured_at": "2026-09-30T00:00:00+00:00", "indexes": [], "tables": []}
    monkeypatch.setenv("MYSQL_URI", "mysql://example")
    monkeypatch.setenv("MONGODB_URL", "mongodb://example")
    monkeypatch.setattr(
        ius, "create_read_only_engine", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(ius, "MongoDBAdapter", MagicMock())
    monkeypatch.setattr(ius, "flush_metrics", MagicMock())

    with patch.object(ius, "run_snapshot", new=AsyncMock(return_value=document)):
        assert ius.main(["--dry-run", "--json"]) == ius.EXIT_OK

    record = next(r for r in caplog.records if r.name == ius.__name__)
    payload = json.loads(record.getMessage().split(" ", 1)[1])
    assert payload["level"] == "INFO"
    assert payload["indexes"] == []
