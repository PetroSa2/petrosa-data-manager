from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from data_manager.api.app import create_app
from data_manager.api.gateway_auth import require_service
from data_manager.api.routes import ingest


def test_full_app_routes_klines_to_ingest_before_generic():
    collection = Mock()
    collection.bulk_write = AsyncMock()
    ingest.set_database_manager(
        SimpleNamespace(
            mongodb_adapter=SimpleNamespace(db={"klines_15m": collection}),
            mysql_adapter=None,
        )
    )
    app = create_app()
    app.dependency_overrides[require_service] = lambda: None

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ingest/klines",
            json={
                "symbol": "BTCUSDT",
                "interval": "15m",
                "klines": [
                    {
                        "timestamp": "2026-09-24T00:00:00Z",
                        "open_price": "1",
                        "high_price": "2",
                        "low_price": "1",
                        "close_price": "2",
                        "volume": "3",
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["symbol"] == "BTCUSDT"
    assert response.json()["mysql_copy"] == "unavailable"
    collection.bulk_write.assert_awaited_once()
