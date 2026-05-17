"""
Unit tests for data_manager.consumer.nats_client.NATSClient.

Mocks nats.connect; verifies happy paths, disconnect-cleanup, subscribe/publish
guards when not connected, and the on_disconnected / on_reconnected callbacks.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.consumer.nats_client import NATSClient


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_sets_state_and_returns_true(self):
        with patch("data_manager.consumer.nats_client.nats.connect") as nc:
            mock_client = MagicMock()
            mock_client.connected_server_version = "2.10"
            nc.return_value = mock_client
            client = NATSClient()
            ok = await client.connect()
            assert ok is True
            assert client.connected is True
            assert client.nc is mock_client

    @pytest.mark.asyncio
    async def test_connect_failure_returns_false(self):
        with patch(
            "data_manager.consumer.nats_client.nats.connect",
            side_effect=RuntimeError("conn refused"),
        ):
            client = NATSClient()
            ok = await client.connect()
            assert ok is False
            assert client.connected is False


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_drains_and_closes(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_nc.drain = AsyncMock()
        mock_nc.close = AsyncMock()
        client.nc = mock_nc
        client.connected = True
        await client.disconnect()
        mock_nc.drain.assert_called_once()
        mock_nc.close.assert_called_once()
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_disconnect_swallows_exceptions(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_nc.drain = AsyncMock(side_effect=RuntimeError("x"))
        mock_nc.close = AsyncMock()
        client.nc = mock_nc
        client.connected = True
        # Must not raise; finally-block still resets state.
        await client.disconnect()
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_not_connected(self):
        client = NATSClient()
        # client.nc is None — disconnect should not call anything.
        await client.disconnect()
        assert client.connected is False


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_returns_none_when_not_connected(self):
        client = NATSClient()
        result = await client.subscribe("subject", lambda m: None)
        assert result is None

    @pytest.mark.asyncio
    async def test_subscribe_returns_subscription_on_success(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_subscription = MagicMock()
        mock_nc.subscribe = AsyncMock(return_value=mock_subscription)
        client.nc = mock_nc
        client.connected = True

        async def cb(m):
            pass

        result = await client.subscribe("subject", cb, queue="q1")
        assert result is mock_subscription
        mock_nc.subscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_returns_none_on_error(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_nc.subscribe = AsyncMock(side_effect=RuntimeError("subscribe failed"))
        client.nc = mock_nc
        client.connected = True
        result = await client.subscribe("subject", lambda m: None)
        assert result is None


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_returns_false_when_not_connected(self):
        client = NATSClient()
        assert await client.publish("subject", b"data") is False

    @pytest.mark.asyncio
    async def test_publish_returns_true_on_success(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_nc.publish = AsyncMock()
        client.nc = mock_nc
        client.connected = True
        assert await client.publish("subject", b"data") is True
        mock_nc.publish.assert_called_once_with("subject", b"data")

    @pytest.mark.asyncio
    async def test_publish_returns_false_on_error(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_nc.publish = AsyncMock(side_effect=RuntimeError("publish failed"))
        client.nc = mock_nc
        client.connected = True
        assert await client.publish("subject", b"data") is False


class TestPublishWithTraceContext:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_connected(self):
        client = NATSClient()
        assert await client.publish_with_trace_context("subject", {"k": "v"}) is False

    @pytest.mark.asyncio
    async def test_injects_trace_context_and_publishes(self):
        with patch(
            "data_manager.consumer.nats_client.NATSTracePropagator.inject_context"
        ) as inject:
            inject.return_value = {"k": "v", "trace_id": "abc"}
            client = NATSClient()
            mock_nc = MagicMock()
            mock_nc.publish = AsyncMock()
            client.nc = mock_nc
            client.connected = True
            ok = await client.publish_with_trace_context("subject", {"k": "v"})
            assert ok is True
            mock_nc.publish.assert_called_once()
            # The published data must be the JSON-encoded enriched dict.
            published_data = mock_nc.publish.call_args[0][1]
            assert b"trace_id" in published_data

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self):
        with patch(
            "data_manager.consumer.nats_client.NATSTracePropagator.inject_context",
            return_value={"k": "v"},
        ):
            client = NATSClient()
            mock_nc = MagicMock()
            mock_nc.publish = AsyncMock(side_effect=RuntimeError("publish failed"))
            client.nc = mock_nc
            client.connected = True
            assert (
                await client.publish_with_trace_context("subject", {"k": "v"}) is False
            )


class TestIsConnected:
    def test_returns_false_when_no_client(self):
        client = NATSClient()
        assert client.is_connected() is False

    def test_returns_false_when_not_connected(self):
        client = NATSClient()
        client.nc = MagicMock()
        client.nc.is_connected = True
        client.connected = False
        assert client.is_connected() is False

    def test_returns_true_when_fully_connected(self):
        client = NATSClient()
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        client.nc = mock_nc
        client.connected = True
        assert client.is_connected() is True


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_on_disconnected_sets_state(self):
        client = NATSClient()
        client.connected = True
        await client._on_disconnected()
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_on_disconnected_invokes_user_cb(self):
        client = NATSClient()
        user_cb = AsyncMock()
        client._disconnect_cb = user_cb
        await client._on_disconnected()
        user_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_reconnected_sets_state(self):
        client = NATSClient()
        client.connected = False
        await client._on_reconnected()
        assert client.connected is True

    @pytest.mark.asyncio
    async def test_on_reconnected_invokes_user_cb(self):
        client = NATSClient()
        user_cb = AsyncMock()
        client._reconnect_cb = user_cb
        await client._on_reconnected()
        user_cb.assert_called_once()
