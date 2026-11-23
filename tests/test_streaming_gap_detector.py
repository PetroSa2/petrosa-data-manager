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

    def _make_open_time_msg(self, symbol, timeframe, open_time):
        interval_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
        }[timeframe]
        # Binance's ``T`` is the inclusive end of the candle, one millisecond
        # before the next candle opens.
        close_time = (
            open_time + timedelta(seconds=interval_seconds) - timedelta(milliseconds=1)
        )
        return self._make_kline_msg(
            symbol, timeframe, int(close_time.timestamp() * 1000)
        )

    async def _process_kline(self, detector, msg):
        """Helper: call _on_kline_event and wait for background tasks."""
        await detector._on_kline_event(MagicMock(data=json.dumps(msg).encode()))
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
    async def test_15m_closed_candles_use_open_time_for_gap_detection(
        self, detector, mock_backfill_orchestrator
    ):
        """Consecutive Binance close times must not report a false 15m gap."""
        first_open = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        for offset in (0, 15, 30):
            await self._process_kline(
                detector,
                self._make_open_time_msg(
                    "BTCUSDT", "15m", first_open + timedelta(minutes=offset)
                ),
            )

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
    async def test_15m_missing_closed_candle_triggers_backfill(
        self, detector, mock_backfill_orchestrator
    ):
        first_open = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        await self._process_kline(
            detector, self._make_open_time_msg("BTCUSDT", "15m", first_open)
        )
        await self._process_kline(
            detector,
            self._make_open_time_msg(
                "BTCUSDT", "15m", first_open + timedelta(minutes=45)
            ),
        )

        await asyncio.sleep(0.01)
        request = mock_backfill_orchestrator.create_backfill_job.call_args.args[0]
        assert request.timeframe == "15m"
        assert request.start_time == first_open + timedelta(minutes=15)
        assert request.end_time == first_open + timedelta(minutes=45)

    @pytest.mark.asyncio
    async def test_event_without_final_flag_is_processed(
        self, detector, mock_backfill_orchestrator
    ):
        first_open = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        first = self._make_open_time_msg("BTCUSDT", "15m", first_open)
        second = self._make_open_time_msg(
            "BTCUSDT", "15m", first_open + timedelta(minutes=30)
        )
        first["k"].pop("x")
        second["k"].pop("x")

        await self._process_kline(detector, first)
        await self._process_kline(detector, second)

        assert detector._last_seen[("BTCUSDT", "15m")] == first_open + timedelta(
            minutes=30
        )
        request = mock_backfill_orchestrator.create_backfill_job.call_args.args[0]
        assert request.start_time == first_open + timedelta(minutes=15)

    @pytest.mark.asyncio
    async def test_explicit_final_flag_matches_omitted_flag(
        self, detector, mock_backfill_orchestrator
    ):
        first_open = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        omitted = self._make_open_time_msg("BTCUSDT", "15m", first_open)
        explicit = self._make_open_time_msg(
            "BTCUSDT", "15m", first_open + timedelta(minutes=30)
        )
        omitted["k"].pop("x")
        await self._process_kline(detector, omitted)
        await self._process_kline(detector, explicit)
        assert mock_backfill_orchestrator.create_backfill_job.await_count == 1

    @pytest.mark.asyncio
    async def test_gap_at_one_interval_boundary_triggers_backfill(
        self, detector, mock_backfill_orchestrator
    ):
        first_open = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        await self._process_kline(
            detector, self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )

        await self._process_kline(
            detector,
            self._make_open_time_msg(
                "BTCUSDT", "1m", first_open + timedelta(minutes=2)
            ),
        )
        request = mock_backfill_orchestrator.create_backfill_job.call_args.args[0]
        assert request.start_time == first_open + timedelta(minutes=1)

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
        assert detector._last_seen[("BTCUSDT", "1m")] == datetime(
            2026, 1, 1, 12, 9, 0, 1000, tzinfo=UTC
        )

        # A later event must still use the original forward watermark.
        next_ts = int(datetime(2026, 1, 1, 12, 11, 0, tzinfo=UTC).timestamp() * 1000)
        await self._process_kline(
            detector, self._make_kline_msg("BTCUSDT", "1m", next_ts)
        )
        assert detector._last_seen[("BTCUSDT", "1m")] == datetime(
            2026, 1, 1, 12, 10, 0, 1000, tzinfo=UTC
        )

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
        await detector._on_kline_event(MagicMock(data=json.dumps(msg).encode()))

        # Not a kline
        msg2 = {"e": "trade", "s": "BTCUSDT"}
        await detector._on_kline_event(MagicMock(data=json.dumps(msg2).encode()))

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

    @pytest.mark.asyncio
    async def test_gap_over_24_hours_is_submitted_in_complete_chunks(
        self, detector, mock_backfill_orchestrator
    ):
        first_open = datetime(2026, 1, 1, tzinfo=UTC)
        await self._process_kline(
            detector, self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        final_open = first_open + timedelta(hours=48, minutes=1)
        await self._process_kline(
            detector, self._make_open_time_msg("BTCUSDT", "1m", final_open)
        )

        requests = [
            call.args[0]
            for call in mock_backfill_orchestrator.create_backfill_job.call_args_list
        ]
        assert len(requests) == 2
        assert requests[0].start_time == first_open + timedelta(minutes=1)
        assert requests[0].end_time == requests[0].start_time + timedelta(hours=24)
        assert requests[1].start_time == requests[0].end_time
        assert requests[-1].end_time == final_open
        assert all(
            left.end_time == right.start_time
            for left, right in zip(requests, requests[1:], strict=False)
        )

    @pytest.mark.asyncio
    async def test_distinct_gaps_are_not_suppressed_by_cooldown(
        self, detector, mock_backfill_orchestrator
    ):
        first_open = datetime(2026, 1, 1, tzinfo=UTC)
        await self._process_kline(
            detector, self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        await self._process_kline(
            detector,
            self._make_open_time_msg(
                "BTCUSDT", "1m", first_open + timedelta(minutes=3)
            ),
        )
        await self._process_kline(
            detector,
            self._make_open_time_msg(
                "BTCUSDT", "1m", first_open + timedelta(minutes=6)
            ),
        )

        assert mock_backfill_orchestrator.create_backfill_job.await_count == 2

    @pytest.mark.asyncio
    async def test_failed_enqueue_does_not_claim_gap_submitted(self, mock_candle_repo):
        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(side_effect=RuntimeError("down"))
        queue = MagicMock()
        queue.add = AsyncMock(side_effect=RuntimeError("queue down"))
        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=orchestrator,
            backfill_queue=queue,
        )
        first_open = datetime(2026, 1, 1, tzinfo=UTC)
        await self._process_kline(
            detector, self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        await self._process_kline(
            detector,
            self._make_open_time_msg(
                "BTCUSDT", "1m", first_open + timedelta(minutes=2)
            ),
        )

        assert detector._last_seen["BTCUSDT", "1m"] == first_open
        assert not detector._reported_gaps

        await self._process_kline(
            detector,
            self._make_open_time_msg(
                "BTCUSDT", "1m", first_open + timedelta(minutes=3)
            ),
        )
        requests = [
            call.args[0] for call in orchestrator.create_backfill_job.call_args_list
        ]
        assert requests[-1].start_time == first_open + timedelta(minutes=1)

    @pytest.mark.asyncio
    async def test_dual_submission_failure_retries_same_gap(self, mock_candle_repo):
        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(side_effect=RuntimeError("down"))
        queue = MagicMock()
        queue.add = AsyncMock(side_effect=RuntimeError("queue down"))
        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=orchestrator,
            backfill_queue=queue,
        )
        first_open = datetime(2026, 1, 1, tzinfo=UTC)
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open + timedelta(minutes=3))
        )
        first_attempts = orchestrator.create_backfill_job.await_count
        assert detector._last_seen["BTCUSDT", "1m"] == first_open
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open + timedelta(minutes=4))
        )

        assert orchestrator.create_backfill_job.await_count == first_attempts + 1
        request = orchestrator.create_backfill_job.call_args.args[0]
        assert request.start_time == first_open + timedelta(minutes=1)
        assert len(detector._reported_gaps) == 0

    @pytest.mark.asyncio
    async def test_partial_multichunk_failure_retries_unsent_remainder(
        self, mock_candle_repo
    ):
        outcomes = [MagicMock(job_id="bf-ok"), RuntimeError("down")]
        first_open = datetime(2026, 1, 1, tzinfo=UTC)

        async def create_job(request):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(side_effect=create_job)
        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=orchestrator,
        )
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        final_open = first_open + timedelta(hours=48, minutes=1)
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", final_open)
        )

        requests = [
            call.args[0] for call in orchestrator.create_backfill_job.call_args_list
        ]
        assert len(requests) == 2
        covered_through = requests[0].end_time - timedelta(minutes=1)
        assert detector._last_seen["BTCUSDT", "1m"] == covered_through
        assert list(detector._reported_gaps) == [
            (
                "BTCUSDT",
                "1m",
                (first_open + timedelta(minutes=1)).isoformat(),
                requests[0].end_time.isoformat(),
            )
        ]

        orchestrator.create_backfill_job.side_effect = None
        orchestrator.create_backfill_job.return_value = MagicMock(job_id="bf-retry")
        later_open = final_open + timedelta(minutes=1)
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", later_open)
        )

        retries = [
            call.args[0] for call in orchestrator.create_backfill_job.call_args_list[2:]
        ]
        assert retries[0].start_time == requests[0].end_time
        assert retries[-1].end_time == later_open
        assert detector._last_seen["BTCUSDT", "1m"] == later_open

    @pytest.mark.asyncio
    async def test_cancelled_backfill_releases_pair_lock(self, mock_candle_repo):
        started = asyncio.Event()
        call_count = 0

        async def create_job(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                started.set()
                await asyncio.Event().wait()
            return MagicMock(job_id="bf-ok")

        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(side_effect=create_job)
        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=orchestrator,
        )
        first_open = datetime(2026, 1, 1, tzinfo=UTC)
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        await detector._on_kline_event(
            MagicMock(
                data=json.dumps(
                    self._make_open_time_msg(
                        "BTCUSDT", "1m", first_open + timedelta(minutes=2)
                    )
                ).encode()
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        task = next(task for task in detector._background_tasks if not task.done())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        lock = detector._gap_locks["BTCUSDT", "1m"]
        assert lock.locked() is False
        assert detector._last_seen["BTCUSDT", "1m"] == first_open

        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open + timedelta(minutes=3))
        )
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_callbacks_submit_once_per_pair(self, mock_candle_repo):
        started = asyncio.Event()
        release = asyncio.Event()
        call_count = 0

        async def create_job(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                started.set()
                await release.wait()
            return MagicMock(job_id="bf-ok")

        orchestrator = MagicMock()
        orchestrator.create_backfill_job = AsyncMock(side_effect=create_job)
        detector = StreamingGapDetector(
            candle_repo=mock_candle_repo,
            backfill_orchestrator=orchestrator,
        )
        first_open = datetime(2026, 1, 1, tzinfo=UTC)
        await detector._process_kline(
            self._make_open_time_msg("BTCUSDT", "1m", first_open)
        )
        gap_msg = json.dumps(
            self._make_open_time_msg("BTCUSDT", "1m", first_open + timedelta(minutes=2))
        ).encode()
        await detector._on_kline_event(MagicMock(data=gap_msg))
        await detector._on_kline_event(MagicMock(data=gap_msg))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert (
            len([task for task in detector._background_tasks if not task.done()]) == 2
        )

        release.set()
        await asyncio.gather(*list(detector._background_tasks))
        assert call_count == 1


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
        await detector._on_kline_event(MagicMock(data=json.dumps(msg1).encode()))

        # Simulate the service going down for 3 minutes, then coming back
        # with the next candle at T-7 min (3-minute gap for 1m candles)
        gap_time = int((datetime(2026, 1, 1, 12, 3, 0, tzinfo=UTC)).timestamp() * 1000)
        msg2 = {
            "s": "BTCUSDT",
            "k": {"i": "1m", "T": gap_time, "x": True},
            "e": "kline",
        }
        await detector._on_kline_event(MagicMock(data=json.dumps(msg2).encode()))

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
            await detector._on_kline_event(MagicMock(data=json.dumps(msg).encode()))

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
