"""
Analytics and computed metrics models.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MetricMetadata(BaseModel):
    """Metadata for computed metrics."""

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})

    method: str = Field(..., description="Computation method used")
    window: str = Field(..., description="Time window for computation")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Computation parameters"
    )
    completeness: float = Field(
        ..., ge=0.0, le=100.0, description="Data completeness for computation"
    )
    computed_at: datetime = Field(..., description="Computation timestamp")


class VolatilityMetrics(BaseModel):
    """Volatility metrics for a symbol."""

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    rolling_stddev: Decimal = Field(..., description="Rolling standard deviation")
    annualized_volatility: Decimal = Field(..., description="Annualized volatility")
    parkinson: Decimal | None = Field(
        None, description="Parkinson volatility estimator"
    )
    garman_klass: Decimal | None = Field(None, description="Garman-Klass volatility")
    volatility_of_volatility: Decimal | None = Field(
        None, description="Volatility of volatility"
    )
    metadata: MetricMetadata = Field(..., description="Computation metadata")


class VolumeMetrics(BaseModel):
    """Volume metrics for a symbol."""

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    total_volume: Decimal = Field(..., description="Total volume")
    volume_sma: Decimal = Field(..., description="Volume simple moving average")
    volume_ema: Decimal = Field(..., description="Volume exponential moving average")
    volume_delta: Decimal = Field(..., description="Buy vs sell volume delta")
    volume_spike_ratio: Decimal = Field(
        ..., description="Volume spike ratio vs baseline"
    )
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )


class SpreadMetrics(BaseModel):
    """Spread and liquidity metrics."""

    symbol: str = Field(..., description="Trading pair symbol")
    bid_ask_spread: Decimal = Field(..., description="Bid-ask spread")
    spread_percentage: Decimal = Field(..., description="Spread as percentage")
    market_depth_bid: Decimal = Field(..., description="Market depth on bid side")
    market_depth_ask: Decimal = Field(..., description="Market depth on ask side")
    liquidity_ratio: Decimal = Field(..., description="Liquidity ratio (Amihud proxy)")
    slippage_estimate: Decimal | None = Field(None, description="Estimated slippage")
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )


class DeviationMetrics(BaseModel):
    """Deviation and dispersion metrics."""

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    standard_deviation: Decimal = Field(..., description="Standard deviation")
    variance: Decimal = Field(..., description="Variance")
    z_score: Decimal = Field(..., description="Z-score (standardized deviation)")
    bollinger_upper: Decimal = Field(..., description="Bollinger upper band")
    bollinger_lower: Decimal = Field(..., description="Bollinger lower band")
    price_range_index: Decimal = Field(..., description="Price range index")
    autocorrelation: Decimal | None = Field(None, description="Serial autocorrelation")
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )


class TrendMetrics(BaseModel):
    """Trend and momentum metrics."""

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    sma: Decimal = Field(..., description="Simple moving average")
    ema: Decimal = Field(..., description="Exponential moving average")
    wma: Decimal = Field(..., description="Weighted moving average")
    rate_of_change: Decimal = Field(..., description="Rate of change")
    directional_strength: Decimal = Field(..., description="Directional strength index")
    crossover_signal: str | None = Field(
        None, description="Crossover signal (bullish/bearish)"
    )
    rolling_beta: Decimal | None = Field(None, description="Rolling beta to benchmark")
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )


class SeasonalityMetrics(BaseModel):
    """Seasonality and cyclical pattern metrics."""

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    hourly_pattern: dict[int, Decimal] = Field(
        ..., description="Hourly recurring patterns (0-23)"
    )
    daily_pattern: dict[int, Decimal] = Field(..., description="Daily patterns (0-6)")
    seasonal_deviation: Decimal = Field(..., description="Deviation from seasonal mean")
    entropy_index: Decimal = Field(..., description="Entropy/randomness index")
    dominant_cycle: int | None = Field(
        None, description="Dominant cycle period in hours"
    )
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )


class CorrelationMetrics(BaseModel):
    """Correlation and cross-market metrics."""

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    correlation_matrix: dict[str, Decimal] = Field(
        ..., description="Pairwise correlation with other symbols"
    )
    rolling_correlation: Decimal = Field(
        ..., description="Rolling correlation to benchmark"
    )
    cross_correlation_lag: int | None = Field(
        None, description="Cross-correlation lag in periods"
    )
    volatility_correlation: Decimal | None = Field(
        None, description="Volatility correlation"
    )
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )


class MarketRegime(BaseModel):
    """Market regime classification."""

    symbol: str = Field(..., description="Trading pair symbol")
    timeframe: str = Field(..., description="Data timeframe")
    regime: str = Field(
        ...,
        description="Market regime (e.g., 'high_volatility_low_volume', 'stable_accumulation')",
    )
    volatility_level: str = Field(..., description="Volatility level (low/medium/high)")
    volume_level: str = Field(..., description="Volume level (low/medium/high)")
    trend_direction: str = Field(
        ..., description="Trend direction (bullish/bearish/neutral)"
    )
    confidence: Decimal = Field(
        ..., ge=0.0, le=1.0, description="Regime confidence score"
    )
    metadata: MetricMetadata = Field(..., description="Computation metadata")

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()}
    )
