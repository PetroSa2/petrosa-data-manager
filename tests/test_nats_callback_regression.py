import ast
import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.subscription import Subscription

from data_manager.auditor.streaming_gap_detector import StreamingGapDetector


def _kline_message() -> dict:
    return {
        "e": "kline",
        "s": "BTCUSDT",
        "k": {"i": "1m", "T": 1_700_000_000_000},
    }


@pytest.fixture
def detector() -> StreamingGapDetector:
    return StreamingGapDetector(candle_repo=MagicMock())


@pytest.mark.asyncio
async def test_kline_callback_is_async_and_processes_parsed_payload(detector):
    assert inspect.iscoroutinefunction(StreamingGapDetector._on_kline_event)
    detector._process_kline = AsyncMock()

    await detector._on_kline_event(
        MagicMock(data=json.dumps(_kline_message()).encode())
    )
    await asyncio.sleep(0)

    detector._process_kline.assert_awaited_once_with(_kline_message())


@pytest.mark.asyncio
async def test_non_kline_event_is_ignored(detector):
    detector._process_kline = AsyncMock()

    await detector._on_kline_event(MagicMock(data=b'{"e":"trade"}'))

    detector._process_kline.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_json_logs_warning_without_raising(detector, caplog):
    with caplog.at_level("WARNING"):
        await detector._on_kline_event(MagicMock(data=b"not-json"))

    assert "Failed to parse kline event" in caplog.text


@pytest.mark.asyncio
async def test_background_task_is_retained_until_done(detector):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_process(data):
        started.set()
        await release.wait()

    detector._process_kline = blocked_process
    await detector._on_kline_event(
        MagicMock(data=str(_kline_message()).replace("'", '"').encode())
    )
    await started.wait()

    assert len(detector._background_tasks) == 1
    release.set()
    await asyncio.gather(*detector._background_tasks)
    await asyncio.sleep(0)
    assert not detector._background_tasks


def test_all_nats_callback_keywords_resolve_to_async_methods():
    package_root = Path(__file__).parents[1] / "data_manager"
    failures = []

    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        async_methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef,))
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                not isinstance(node.func, ast.Attribute)
                or node.func.attr != "subscribe"
            ):
                continue
            callback = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "callback"
                ),
                None,
            )
            if (
                isinstance(callback, ast.Attribute)
                and callback.attr not in async_methods
            ):
                failures.append(f"{path}:{node.lineno}: {callback.attr}")

    assert not failures, "Non-coroutine NATS callbacks: " + ", ".join(failures)


@pytest.mark.asyncio
async def test_nats_py_accepts_detector_callback_and_rejects_plain_function(detector):
    subscription = Subscription(MagicMock(), 1, "subject", cb=detector._on_kline_event)
    subscription._start(lambda error: None)
    task = subscription._wait_for_msgs_task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    def plain_callback(message):
        return None

    invalid_subscription = Subscription(MagicMock(), 1, "subject", cb=plain_callback)
    with pytest.raises(Exception, match="must use coroutine"):
        invalid_subscription._start(lambda error: None)
