"""In-process klines retention for the long-lived MongoDB application client."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from data_manager.db.mongodb_adapter import MongoDBAdapter
from data_manager.maintenance.klines_retention import (
    MongoRetentionBackend,
    RetentionConfig,
    load_config_from_env,
    prune_klines,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(float(os.getenv(name, str(default))), minimum)
    except ValueError:
        return default


async def run_shared_client_retention(
    adapter: MongoDBAdapter,
    config: RetentionConfig,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run bounded retention without replacing or disconnecting the shared client."""
    for attempt in range(config.mongo_retries + 1):
        try:
            await prune_klines(MongoRetentionBackend(adapter), config)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            if attempt >= config.mongo_retries:
                raise
            delay = config.mongo_retry_backoff_seconds * (attempt + 1)
            logger.warning(
                "klines_retention: shared-client attempt %d/%d failed; "
                "retrying in %.1fs",
                attempt + 1,
                config.mongo_retries + 1,
                delay,
                exc_info=True,
            )
            await sleep(delay)


async def retention_loop(
    adapter_provider: Callable[[], MongoDBAdapter | None],
    shutdown_event: asyncio.Event,
    *,
    is_leader: Callable[[], bool],
    interval_seconds: float | None = None,
    config: RetentionConfig | None = None,
) -> None:
    """Run retention immediately and then at a bounded six-hour interval."""
    interval = interval_seconds or _env_float(
        "KLINES_RETENTION_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, minimum=1.0
    )
    resolved_config = config or load_config_from_env()

    while not shutdown_event.is_set():
        if is_leader():
            adapter = adapter_provider()
            if adapter is not None and getattr(adapter, "_connected", True):
                try:
                    await run_shared_client_retention(adapter, resolved_config)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("klines_retention: shared-client cycle failed")
            else:
                logger.warning("klines_retention: shared Mongo adapter unavailable")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except TimeoutError:
            continue
