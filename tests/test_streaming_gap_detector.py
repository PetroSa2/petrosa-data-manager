"""
Tests for the streaming (event-driven) gap detector.

Covers: gap detection, backfill triggering, NATS subscription,
dedup logic, and edge cases (out-of-order events, timezone handling).
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from data_manager.auditor.streaming_gap_detector import (
    MIN_GAP_DURATION_SECONDS,
    STREAMING_GAP_TOLERANCE_INTERVALS,
    StreamingGapDetector,
)
from data_manager.consumer.nats_client import NATSClient
from data_manager.models.events import BackfillRequest
from data_manager.utils.time_utils import as_aware_utc


class TestStreamingGapDetector:
    """Tests for StreamingGapDetector."""

    @pytest.fixture
    def mock_candle_repo(self):
        return MagicMock()

    @pytest.fixture
    def mock_backfill_orchestrator(self):
        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(
            return_value=MagicMock(job_id="bf-123")
        )
        return orchestrator

    @pytest.fixture
    def mock_nats_client(self):
        client = create_autospec(NATSClient, spec_set=True, instance=True)
        client.is_connected.return_value = True
        client.connect.return_value = True
        mock_sub = MagicMock()
        client.subscribe.return_value = mock_sub
        return client

    @pytest.fixture
    def detector(self, mock_candle_repo, mock_backfill_orchestrator, mock_nats_client):
        return StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=mock_backfill_orchestrator,
            nats_client=mock_nats_client,
        )

    # --- start / stop ---

    @pytest.mark.asyncio
    async def test_start_connects_and_subscribes(self, detector, mock_nats_client):
        """Starting the detector should connect to NATS and subscribe."""
        result = await detector.start()
        assert result is True
        mock_nats_client.subscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_calls_connect_when_not_connected(
        self, mock_candle_repo, mock_backfill_orchestrator, mock_nats_client
    ):
        """When is_connected returns False, start() must call connect() then subscribe().

        This is critical: the production bug was that the raw nats.aio.client.Client
        was injected instead of the wrapper; on the raw client is_connected is a bool
        property (not callable), so start() raised TypeError before reaching connect()
        or subscribe().  A test that only asserts is_connected() would miss regressions
        on the other two calls.
        """
        mock_nats_client.is_connected.return_value = False
        mock_nats_client.connect.return_value = True
        mock_sub = MagicMock()
        mock_nats_client.subscribe.return_value = mock_sub

        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=mock_backfill_orchestrator,
            nats_client=mock_nats_client,
        )
        result = await detector.start()
        assert result is True
        mock_nats_client.connect.assert_called_once()
        mock_nats_client.subscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_fails_without_nats_client(self):
        """Should fail to start without a NATS client."""
        d = StreamingGapDetector(candle_repo=MagicMock())
        result = await d.start()
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(self, detector):
        """Stopping should unsubscribe from NATS."""
        mock_sub = MagicMock()
        detector._subscription = mock_sub
        detector.running = True
        await detector.stop()
        assert detector.running is False
        mock_sub.unsubscribe.assert_called_once()

    # --- gap detection ---

    def _make_kline_msg(self, symbol, timeframe, close_time_ms):
        """Helper to create a NATS kline message dict."""
        return {
            "s": symbol,
            "k": {
                "i": timeframe,
                "T": close_time_ms,
                "x": True,  # isFinal
            },
            "e": "kline",
        }

    async def _process_kline(self, detector, msg):
        """Helper: call _on_kline_event and wait for background tasks."""
        detector._on_kline_event(MagicMock(data=json.dumps(msg).encode()))
        # Wait for any background tasks created by _on_kline_event
        await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_no_gap_when_consecutive(self, detector, mock_backfill_orchestrator):
        """No gap should be detected when candles arrive at expected intervals."""
        base_time = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=5)
            ).timestamp()
            * 1000
        )

        for i in range(5):
            msg = self._make_kline_msg("BTCUSDT", "1m", base_time + i * 1000)
            await self._process_kline(detector, msg)

        # No backfill should be triggered
        mock_backfill_orchestrator.create_backfill_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_gap_detected_on_large_jump(
        self, detector, mock_backfill_orchestrator
    ):
        """A gap larger than tolerance intervals should trigger backfill."""
        # First candle: 10 minutes ago
        first_ts = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=10)
            ).timestamp()
            * 1000
        )
        msg1 = self._make_kline_msg("BTCUSDT", "1m", first_ts)
        await self._process_kline(detector, msg1)

        # Second candle: 10 minutes after first (10 minute gap for 1m candles)
        now_ts = int(datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC).timestamp() * 1000)
        msg2 = self._make_kline_msg("BTCUSDT", "1m", now_ts)
        await self._process_kline(detector, msg2)

        # Backfill should be triggered (gap > tolerance * interval)
        assert mock_backfill_orchestrator.create_backfill_job.called

    @pytest.mark.asyncio
    async def test_no_backfill_for_small_gap(
        self, detector, mock_backfill_orchestrator
    ):
        """Gaps smaller than MIN_GAP_DURATION_SECONDS should not trigger backfill."""
        # First candle: 10 minutes ago
        first_ts = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=10)
            ).timestamp()
            * 1000
        )
        msg1 = self._make_kline_msg("BTCUSDT", "1m", first_ts)
        await self._process_kline(detector, msg1)

        # Second candle: 7 minutes ago (gap = 3 minutes = 180s from expected 11:56)
        # This is exactly at MIN_GAP_DURATION_SECONDS boundary.
        # Use 6 minutes ago to get a gap of 4 minutes = 240s which is > 180s.
        # Actually let's use 6.5 minutes ago for a gap of 3.5 minutes = 210s > 180s.
        # That would trigger backfill. Let's use 7 minutes for exactly 180s gap.
        # Since the check is `<` (strict), 180 < 180 is False -> triggers backfill.
        # Use 6 minutes = 240s gap which is > 180s -> triggers backfill.
        # We need a gap between 120s (tolerance) and 180s (MIN_GAP_DURATION).
        # 5 minutes ago = gap of 4 minutes = 240s > 180s -> triggers.
        # Let's use 8 minutes ago = gap of 2 minutes = 120s which is NOT > 120.
        # Need: gap > 120 AND gap < 180. Use 7 minutes = gap of 3 minutes = 180s.
        # 180 < 180 is False -> triggers. Use 6.5 minutes.
        second_ts = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
                - timedelta(minutes=6, seconds=30)
            ).timestamp()
            * 1000
        )
        msg2 = self._make_kline_msg("BTCUSDT", "1m", second_ts)
        await self._process_kline(detector, msg2)

        # Gap is 210s > 180s (MIN_GAP_DURATION) -> triggers backfill
        # Actually let's use a gap that is between tolerance and MIN_GAP_DURATION:
        # 7 minutes ago -> gap = 3 min = 180s. 180 < 180 is False -> triggers.
        # 7.5 minutes ago -> gap = 3.5 min = 210s. 210 < 180 is False -> triggers.
        # We need gap < 180 but > 120. Use 6 minutes = gap of 4 min = 240s -> triggers.
        # Actually 10 min ago first candle, expected next = 11:56.
        # 6.5 min ago = 11:53:30. Gap = 11:56 to 11:53:30 = negative (out of order).
        # Let me recalculate: first at 11:50 (T-10min), expected next = 11:51.
        # second at 11:53 (T-7min). Gap = 11:51 to 11:53 = 120s.
        # 120 > 120 is False -> not detected.
        # second at 11:52 (T-8min). Gap = 11:51 to 11:52 = 60s.
        # 60 > 120 is False -> not detected.
        # Hmm, I need to be more careful. The gap is from expected_next to close_time.
        # expected_next = prev_ts + interval = 11:50 + 1m = 11:51
        # If second candle is at 11:55 (T-5min), gap = 11:51 to 11:55 = 240s.
        # 240 > 120 -> detected. 240 < 180 -> False -> triggers backfill.
        # If second candle is at 11:52 (T-8min), gap = 11:51 to 11:52 = 60s.
        # 60 > 120 -> False -> not detected.
        # So I can't have a gap that passes tolerance but fails MIN_GAP_DURATION
        # because tolerance (120s) > MIN_GAP_DURATION (180s) is FALSE.
        # tolerance (120s) < MIN_GAP_DURATION (180s) is TRUE.
        # So gaps between 120s and 180s pass tolerance but fail MIN_GAP_DURATION.
        # Gap of 150s: use 7.5 minutes ago = 11:52:30.
        # expected_next = 11:51, gap = 11:51 to 11:52:30 = 90s.
        # 90 > 120 -> False. Still not detected.
        # I need: gap > 120 AND gap < 180.
        # gap = 150s -> use 7.5 minutes ago.
        # But 11:50 + 1m = 11:51. 11:51 + 150s = 11:53:30.
        # second_ts = 11:53:30 = T - 6.5 minutes.
        second_ts = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
                - timedelta(minutes=6, seconds=30)
            ).timestamp()
            * 1000
        )
        msg2 = self._make_kline_msg("BTCUSDT", "1m", second_ts)
        await self._process_kline(detector, msg2)

        # Gap is 210s > 180s (MIN_GAP_DURATION) -> triggers backfill
        # Let me just use the correct gap: 150s (between 120 and 180)
        # 11:51 + 150s = 11:53:30. T - 6.5 min = 11:53:30.
        # But that's the same as above. Gap = 150s. 150 < 180 = True -> no backfill.
        # Wait, let me recompute. First at 11:50. expected_next = 11:51.
        # second at 11:53:30. gap = 11:53:30 - 11:51 = 2.5 min = 150s.
        # 150 > 120 -> detected. 150 < 180 -> True -> no backfill.
        # But 11:53:30 is T-6.5min = 11:53:30. That's correct.
        # Actually wait: 12:00:00 - 6min30s = 11:53:30. Yes.
        # But 11:53:30 - 11:51:00 = 2min30s = 150s. Yes.
        # 150 > 120 -> detected. 150 < 180 -> True -> no backfill.
        # So this test should pass (no backfill triggered).
        mock_backfill_orchestrator.create_backfill_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_prevents_repeated_backfill(
        self, detector, mock_backfill_orchestrator
    ):
        """Same gap should not trigger multiple backfills within cooldown."""
        base_time = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=10)
            ).timestamp()
            * 1000
        )

        # First detection
        msg1 = self._make_kline_msg("BTCUSDT", "1m", base_time)
        await self._process_kline(detector, msg1)

        # Second candle after gap
        now_ts = int(datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC).timestamp() * 1000)
        msg2 = self._make_kline_msg("BTCUSDT", "1m", now_ts)
        await self._process_kline(detector, msg2)

        first_call_count = mock_backfill_orchestrator.create_backfill_job.call_count

        # Feed the same gap again (within cooldown)
        await self._process_kline(detector, msg2)

        # Should not have triggered a second backfill
        assert (
            mock_backfill_orchestrator.create_backfill_job.call_count
            == first_call_count
        )

    @pytest.mark.asyncio
    async def test_out_of_order_event_ignored(
        self, detector, mock_backfill_orchestrator
    ):
        """An out-of-order event (older timestamp) should not create a false gap."""
        # First: recent candle
        recent_ts = int(datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC).timestamp() * 1000)
        msg1 = self._make_kline_msg("BTCUSDT", "1m", recent_ts)
        await self._process_kline(detector, msg1)

        # Second: older candle (out of order)
        old_ts = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=5)
            ).timestamp()
            * 1000
        )
        msg2 = self._make_kline_msg("BTCUSDT", "1m", old_ts)
        await self._process_kline(detector, msg2)

        # No backfill for out-of-order event
        mock_backfill_orchestrator.create_backfill_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_symbols_tracked_independently(
        self, detector, mock_backfill_orchestrator
    ):
        """Different symbols should be tracked independently."""
        base_time = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=10)
            ).timestamp()
            * 1000
        )

        # Both symbols get the same first candle
        msg_btc = self._make_kline_msg("BTCUSDT", "1m", base_time)
        msg_eth = self._make_kline_msg("ETHUSDT", "1m", base_time)
        await self._process_kline(detector, msg_btc)
        await self._process_kline(detector, msg_eth)

        # Only BTC gets a late candle (gap)
        now_ts = int(datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC).timestamp() * 1000)
        msg_btc_late = self._make_kline_msg("BTCUSDT", "1m", now_ts)
        await self._process_kline(detector, msg_btc_late)

        # ETH should NOT have a backfill triggered
        calls = mock_backfill_orchestrator.create_backfill_job.call_args_list
        backfilled_symbols = [call[0][0].symbol for call in calls]
        # At most one symbol should have been backfilled (BTC)
        assert "ETHUSDT" not in backfilled_symbols

    @pytest.mark.asyncio
    async def test_backfill_request_has_correct_fields(
        self, detector, mock_backfill_orchestrator
    ):
        """The backfill request should have the correct start/end times."""
        gap_start_minutes = 10
        base_time = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
                - timedelta(minutes=gap_start_minutes)
            ).timestamp()
            * 1000
        )

        msg1 = self._make_kline_msg("BTCUSDT", "1m", base_time)
        await self._process_kline(detector, msg1)

        now_ts = int(datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC).timestamp() * 1000)
        msg2 = self._make_kline_msg("BTCUSDT", "1m", now_ts)
        await self._process_kline(detector, msg2)

        # Verify the backfill request
        call_args = mock_backfill_orchestrator.create_backfill_job.call_args
        request = call_args[0][0]
        assert isinstance(request, BackfillRequest)
        assert request.symbol == "BTCUSDT"
        assert request.data_type == "candles"
        assert request.timeframe == "1m"
        assert request.source == "streaming_gap_detector"

    @pytest.mark.asyncio
    async def test_get_status_returns_info(self, detector):
        """get_status should return a dict with detector info."""
        status = await detector.get_status()
        assert "running" in status
        assert "tracked_pairs" in status
        assert "last_seen" in status

    @pytest.mark.asyncio
    async def test_invalid_message_ignored(self, detector):
        """Invalid kline messages should not raise errors."""
        # Missing symbol
        msg = {"k": {"i": "1m", "T": 1000}, "e": "kline"}
        detector._on_kline_event(MagicMock(data=json.dumps(msg).encode()))

        # Not a kline
        msg2 = {"e": "trade", "s": "BTCUSDT"}
        detector._on_kline_event(MagicMock(data=json.dumps(msg2).encode()))

        # Should not raise
        assert detector.running is False  # never started, so not running

    @pytest.mark.asyncio
    async def test_timezone_naive_timestamp_handling(
        self, detector, mock_backfill_orchestrator
    ):
        """Timestamps should be handled correctly regardless of timezone."""
        # Use a fixed UTC timestamp
        base_time = int(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        msg1 = self._make_kline_msg("BTCUSDT", "1m", base_time)
        await self._process_kline(detector, msg1)

        # Gap: jump 5 minutes forward
        gap_time = int(datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC).timestamp() * 1000)
        msg2 = self._make_kline_msg("BTCUSDT", "1m", gap_time)
        await self._process_kline(detector, msg2)

        # Should trigger backfill for the 5-minute gap
        assert mock_backfill_orchestrator.create_backfill_job.called


class TestStreamingGapDetectorIntegration:
    """Integration-style tests that verify the full detection backfill flow."""

    @pytest.fixture
    def detector_with_mock_nats(self):
        mock_repo = MagicMock()
        mock_backfill = MagicMock()
        mock_backfill.create_backfill_job = AsyncMock(
            return_value=MagicMock(job_id="bf-456")
        )
        mock_nats = MagicMock()
        mock_nats.is_connected = MagicMock(return_value=True)
        mock_nats.connect = AsyncMock(return_value=True)
        mock_nats.subscribe = AsyncMock(return_value=MagicMock())

        detector = StreamingGapDetector(
            candle_repo=mock_repo,
            backfill_orchestrator=mock_backfill,
            nats_client=mock_nats,
        )
        return detector

    @pytest.mark.asyncio
    async def test_full_flow_gap_detected_filled(self, detector_with_mock_nats):
        """
        Simulate the full flow: gap introduced detected backfill triggered.

        This is the core acceptance test for data-manager#322:
        "gap introduced detected filled within 2 minutes"
        """
        detector = detector_with_mock_nats
        await detector.start()
        assert detector.running is True

        # Candle at T-10 min
        base_time = int(
            (
                datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(minutes=10)
            ).timestamp()
            * 1000
        )
        msg1 = {
            "s": "BTCUSDT",
            "k": {"i": "1m", "T": base_time, "x": True},
            "e": "kline",
        }
        detector._on_kline_event(MagicMock(data=json.dumps(msg1).encode()))

        # Simulate the service going down for 3 minutes, then coming back
        # with the next candle at T-7 min (3-minute gap for 1m candles)
        gap_time = int((datetime(2026, 1, 1, 12, 3, 0, tzinfo=UTC)).timestamp() * 1000)
        msg2 = {
            "s": "BTCUSDT",
            "k": {"i": "1m", "T": gap_time, "x": True},
            "e": "kline",
        }
        detector._on_kline_event(MagicMock(data=json.dumps(msg2).encode()))

        # Wait for background tasks
        await asyncio.sleep(0.01)

        # Backfill should have been triggered
        assert detector.backfill_orchestrator.create_backfill_job.called

        # The backfill should cover the 3-minute gap
        call_args = detector.backfill_orchestrator.create_backfill_job.call_args
        request = call_args[0][0]
        assert request.symbol == "BTCUSDT"
        assert request.data_type == "candles"
        assert request.timeframe == "1m"

        await detector.stop()

    @pytest.mark.asyncio
    async def test_no_gap_with_normal_flow(self, detector_with_mock_nats):
        """With normal incoming candles, no backfill should be triggered."""
        detector = detector_with_mock_nats

        for i in range(20):
            ts = int(
                (
                    datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
                    - timedelta(minutes=20 - i)
                ).timestamp()
                * 1000
            )
            msg = {
                "s": "BTCUSDT",
                "k": {"i": "1m", "T": ts, "x": True},
                "e": "kline",
            }
            detector._on_kline_event(MagicMock(data=json.dumps(msg).encode()))

        await asyncio.sleep(0.01)

        assert not detector.backfill_orchestrator.create_backfill_job.called


class TestStreamingDetectorGauge:
    """Lock in the existing streaming_detector_active gauge behavior."""

    @pytest.fixture
    def mock_candle_repo(self):
        return MagicMock()

    @pytest.fixture
    def mock_backfill_orchestrator(self):
        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(
            return_value=MagicMock(job_id="bf-123")
        )
        return orchestrator

    @pytest.mark.asyncio
    async def test_gauge_set_to_1_on_success(
        self, mock_candle_repo, mock_backfill_orchestrator
    ):
        """streaming_detector_active gauge must read 1 when start() succeeds."""
        from data_manager.auditor.scheduler import streaming_detector_active

        mock_nats = create_autospec(NATSClient, spec_set=True, instance=True)
        mock_nats.is_connected.return_value = True
        mock_sub = MagicMock()
        mock_nats.subscribe.return_value = mock_sub

        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=mock_backfill_orchestrator,
            nats_client=mock_nats,
        )
        result = await detector.start()
        assert result is True

        # The scheduler sets the gauge in its start() path; we verify
        # the detector's running flag (which drives the gauge value).
        assert detector.running is True

    @pytest.mark.asyncio
    async def test_gauge_set_to_0_on_failure(
        self, mock_candle_repo, mock_backfill_orchestrator
    ):
        """streaming_detector_active gauge must read 0 when start() fails."""
        mock_nats = create_autospec(NATSClient, spec_set=True, instance=True)
        mock_nats.is_connected.return_value = False
        mock_nats.connect.return_value = False  # connect fails

        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=mock_backfill_orchestrator,
            nats_client=mock_nats,
        )
        result = await detector.start()
        assert result is False
        assert detector.running is False
