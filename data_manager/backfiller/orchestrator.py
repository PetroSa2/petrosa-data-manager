"""
Backfill orchestrator for managing data recovery jobs.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from decimal import Decimal

from data_manager.backfiller.binance_client import BinanceClient
from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import (
    BackfillRepository,
    CandleRepository,
    FundingRepository,
)
from data_manager.models.events import BackfillJob, BackfillRequest
from data_manager.models.market_data import Candle, FundingRate
from data_manager.utils.time_utils import create_time_chunks, parse_timeframe_to_minutes

logger = logging.getLogger(__name__)


class BackfillOrchestrator:
    """
    Orchestrates backfill jobs to fetch and store missing data.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize backfill orchestrator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.binance_client = BinanceClient()
        self.candle_repo = CandleRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.funding_repo = FundingRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )
        self.backfill_repo = BackfillRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        await self.binance_client.close()

    async def create_backfill_job(self, request: BackfillRequest) -> BackfillJob:
        """
        Create a new backfill job.

        Args:
            request: Backfill request

        Returns:
            Created BackfillJob
        """
        job = BackfillJob(
            job_id=str(uuid.uuid4()),
            request=request,
            status="pending",
            created_at=datetime.utcnow(),
        )

        # Store job in MySQL
        await self.backfill_repo.create_job(job.model_dump())

        # Start execution in background
        asyncio.create_task(self.execute_backfill(job.job_id))

        return job

    async def execute_backfill(self, job_id: str) -> None:
        """
        Execute a backfill job.

        Args:
            job_id: Job identifier
        """
        try:
            logger.info(f"Starting backfill job {job_id}")

            # Get job from database
            job_data = self.backfill_repo.get_job(job_id)
            if not job_data:
                logger.error(f"Job {job_id} not found")
                return

            # Update status to running
            await self.backfill_repo.update_status(job_id, "running")

            # Extract job details
            symbol = job_data["symbol"]
            data_type = job_data["data_type"]
            timeframe = job_data.get("timeframe")
            start_time = job_data["start_time"]
            end_time = job_data["end_time"]

            # Execute backfill based on data type
            if data_type == "candles":
                await self._backfill_candles(
                    job_id, symbol, timeframe, start_time, end_time
                )
            elif data_type == "funding":
                await self._backfill_funding(job_id, symbol, start_time, end_time)
            else:
                logger.error(f"Unsupported data type: {data_type}")
                await self.backfill_repo.update_status(
                    job_id, "failed", error=f"Unsupported data type: {data_type}"
                )
                return

            # Mark job as completed
            await self.backfill_repo.update_status(job_id, "completed")
            logger.info(f"Backfill job {job_id} completed successfully")

        except Exception as e:
            logger.error(f"Error executing backfill job {job_id}: {e}", exc_info=True)
            await self.backfill_repo.update_status(job_id, "failed", error=str(e))

    async def _backfill_candles(
        self,
        job_id: str,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Backfill candle data."""
        logger.info(
            f"Backfilling candles for {symbol} {timeframe} "
            f"from {start_time} to {end_time}"
        )

        # Create chunks (Binance API limit is 1000 per request)
        interval_minutes = parse_timeframe_to_minutes(timeframe)
        chunk_size_minutes = interval_minutes * 1000  # 1000 candles per chunk

        chunks = create_time_chunks(start_time, end_time, chunk_size_minutes)

        total_fetched = 0
        total_inserted = 0

        for chunk_start, chunk_end in chunks:
            try:
                # Fetch from Binance
                klines = await self.binance_client.get_klines(
                    symbol, timeframe, chunk_start, chunk_end, limit=1000
                )

                if not klines:
                    continue

                # Convert to Candle models
                candles = []
                for kline in klines:
                    candle = Candle(
                        symbol=symbol,
                        timestamp=datetime.fromtimestamp(kline[0] / 1000.0),
                        open=Decimal(str(kline[1])),
                        high=Decimal(str(kline[2])),
                        low=Decimal(str(kline[3])),
                        close=Decimal(str(kline[4])),
                        volume=Decimal(str(kline[5])),
                        quote_volume=Decimal(str(kline[7])),
                        trades_count=int(kline[8]),
                        timeframe=timeframe,
                    )
                    candles.append(candle)

                # Insert via repository
                inserted = await self.candle_repo.insert_batch(candles)
                total_fetched += len(candles)
                total_inserted += inserted

                logger.debug(
                    f"Backfilled {inserted}/{len(candles)} candles for chunk "
                    f"{chunk_start} to {chunk_end}"
                )

            except Exception as e:
                logger.error(
                    f"Error backfilling chunk {chunk_start} to {chunk_end}: {e}"
                )

        logger.info(
            f"Backfill complete for {symbol} {timeframe}: "
            f"fetched={total_fetched}, inserted={total_inserted}"
        )

    async def _backfill_funding(
        self,
        job_id: str,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Backfill funding rate data."""
        logger.info(
            f"Backfilling funding rates for {symbol} from {start_time} to {end_time}"
        )

        try:
            # Fetch from Binance
            funding_data = await self.binance_client.get_funding_rate(
                symbol, start_time, end_time, limit=1000
            )

            if not funding_data:
                logger.warning(f"No funding data returned for {symbol}")
                return

            # Convert to FundingRate models
            funding_rates = []
            for rate_data in funding_data:
                funding = FundingRate(
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(rate_data["fundingTime"] / 1000.0),
                    funding_rate=Decimal(str(rate_data["fundingRate"])),
                    mark_price=(
                        Decimal(str(rate_data.get("markPrice", "0")))
                        if rate_data.get("markPrice")
                        else None
                    ),
                )
                funding_rates.append(funding)

            # Insert via repository
            inserted = await self.funding_repo.insert_batch(funding_rates)

            logger.info(
                f"Backfill complete for {symbol} funding rates: "
                f"fetched={len(funding_rates)}, inserted={inserted}"
            )

        except Exception as e:
            logger.error(f"Error backfilling funding rates: {e}", exc_info=True)
