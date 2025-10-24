"""
Spread and liquidity metrics calculator.
"""

import logging
from datetime import datetime
from decimal import Decimal

from data_manager.db.database_manager import DatabaseManager
from data_manager.db.repositories import DepthRepository
from data_manager.models.analytics import MetricMetadata, SpreadMetrics

logger = logging.getLogger(__name__)


class SpreadCalculator:
    """Calculates spread and liquidity metrics from order book depth data."""

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize spread calculator.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager
        self.depth_repo = DepthRepository(
            db_manager.mysql_adapter, db_manager.mongodb_adapter
        )

    async def calculate_spread(self, symbol: str) -> SpreadMetrics | None:
        """
        Calculate spread and liquidity metrics for a symbol.

        Args:
            symbol: Trading pair symbol

        Returns:
            SpreadMetrics or None if insufficient data
        """
        try:
            # Get latest order book depth
            depth_data = await self.depth_repo.get_latest(symbol, limit=1)

            if (
                not depth_data
                or not depth_data[0].get("bids")
                or not depth_data[0].get("asks")
            ):
                logger.warning(f"Insufficient depth data for {symbol}")
                return None

            depth = depth_data[0]
            bids = depth.get("bids", [])
            asks = depth.get("asks", [])

            if not bids or not asks:
                return None

            # Extract best bid/ask
            best_bid_price = Decimal(str(bids[0].get("price", 0)))
            best_ask_price = Decimal(str(asks[0].get("price", 0)))

            # Calculate spread
            bid_ask_spread = best_ask_price - best_bid_price
            mid_price = (best_bid_price + best_ask_price) / 2
            spread_percentage = (
                (bid_ask_spread / mid_price * 100) if mid_price > 0 else Decimal("0")
            )

            # Calculate market depth (sum volumes within 1% of mid price)
            threshold_bid = mid_price * Decimal("0.99")
            threshold_ask = mid_price * Decimal("1.01")

            market_depth_bid = sum(
                Decimal(str(level.get("quantity", 0)))
                for level in bids
                if Decimal(str(level.get("price", 0))) >= threshold_bid
            )

            market_depth_ask = sum(
                Decimal(str(level.get("quantity", 0)))
                for level in asks
                if Decimal(str(level.get("price", 0))) <= threshold_ask
            )

            # Liquidity ratio (placeholder - needs volume and volatility)
            liquidity_ratio = Decimal("0")  # TODO: Volume / Volatility

            # Slippage estimate (VWAP deviation for common order sizes)
            slippage_estimate = self._calculate_slippage(bids, asks, mid_price)

            # Order book imbalance (calculated but not used in response yet)
            # total_bid_volume = sum(Decimal(str(level.get("quantity", 0))) for level in bids[:10])
            # total_ask_volume = sum(Decimal(str(level.get("quantity", 0))) for level in asks[:10])
            # order_book_imbalance = (
            #     (total_bid_volume - total_ask_volume) / (total_bid_volume + total_ask_volume)
            #     if (total_bid_volume + total_ask_volume) > 0
            #     else Decimal("0")
            # )

            # Create metadata
            metadata = MetricMetadata(
                method="order_book_analysis",
                window="snapshot",
                parameters={"depth_levels": len(bids)},
                completeness=100.0,
                computed_at=datetime.utcnow(),
            )

            # Create metrics object
            metrics = SpreadMetrics(
                symbol=symbol,
                bid_ask_spread=bid_ask_spread,
                spread_percentage=spread_percentage,
                market_depth_bid=market_depth_bid,
                market_depth_ask=market_depth_ask,
                liquidity_ratio=liquidity_ratio,
                slippage_estimate=slippage_estimate,
                metadata=metadata,
            )

            # Store in MongoDB
            collection = f"analytics_{symbol}_spread"
            await self.db_manager.mongodb_adapter.write([metrics], collection)

            logger.info(
                f"Spread calculated for {symbol}: "
                f"spread={float(bid_ask_spread):.8f}, "
                f"spread_pct={float(spread_percentage):.4f}%"
            )

            return metrics

        except Exception as e:
            logger.error(f"Error calculating spread for {symbol}: {e}", exc_info=True)
            return None

    def _calculate_slippage(
        self,
        bids: list,
        asks: list,
        mid_price: Decimal,
        order_size: Decimal = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculate estimated slippage for a given order size.

        Args:
            bids: List of bid levels
            asks: List of ask levels
            mid_price: Mid price
            order_size: Order size in base currency

        Returns:
            Estimated slippage percentage
        """
        try:
            # Calculate VWAP for buying order_size
            remaining = order_size
            total_cost = Decimal("0")

            for level in asks:
                price = Decimal(str(level.get("price", 0)))
                quantity = Decimal(str(level.get("quantity", 0)))

                if remaining <= 0:
                    break

                fill_qty = min(remaining, quantity)
                total_cost += fill_qty * price
                remaining -= fill_qty

            if order_size > remaining:
                vwap = total_cost / (order_size - remaining)
                slippage_pct = (vwap - mid_price) / mid_price * 100
                return slippage_pct
            else:
                return Decimal("0")

        except Exception as e:
            logger.error(f"Error calculating slippage: {e}")
            return Decimal("0")
