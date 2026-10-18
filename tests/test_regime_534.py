"""Calibrated confidence of the transitional regime and the exposed classifier inputs (#534)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_manager.analytics.regime import (
    VOL_HIGH,
    VOL_LOW,
    VOLUME_HIGH,
    VOLUME_LOW,
    RegimeClassifier,
    level_clarity,
    transitional_confidence,
)

# --- the formula at its boundaries ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.5, 0.0),  # on the high threshold: still medium, no clarity
        (0.2, 0.0),  # on the low threshold
        (0.35, 1.0),  # the middle of the medium band
        (0.425, 0.5),  # halfway between the middle and the high threshold
        (0.75, 0.5),  # high: 50% past the threshold
        (1.0, 1.0),  # high: a full threshold past (capped)
        (5.0, 1.0),
        (0.1, 0.5),  # low: halfway below the threshold
        (0.0, 1.0),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (None, 0.0),
    ],
)
def test_volatility_clarity_boundaries(value, expected):
    assert level_clarity(value, VOL_LOW, VOL_HIGH) == pytest.approx(expected)


def test_volume_clarity_uses_its_own_thresholds():
    assert level_clarity(1.1, VOLUME_LOW, VOLUME_HIGH) == pytest.approx(1.0)
    assert level_clarity(1.5, VOLUME_LOW, VOLUME_HIGH) == 0.0
    assert level_clarity(0.7, VOLUME_LOW, VOLUME_HIGH) == 0.0
    assert level_clarity(2.25, VOLUME_LOW, VOLUME_HIGH) == pytest.approx(0.5)
    assert level_clarity(0.35, VOLUME_LOW, VOLUME_HIGH) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("vol", "volume", "expected"),
    [
        (0.0, 0.0, 0.5),
        (1.0, 1.0, 0.9),
        (0.6, 1.0, 0.74),  # the weaker input decides
        (1.0, 0.25, 0.6),
        (-3.0, 7.0, 0.5),  # out-of-range clarity is clamped
        (2.0, 2.0, 0.9),
    ],
)
def test_transitional_confidence_is_the_floor_plus_the_weaker_clarity(
    vol, volume, expected
):
    assert transitional_confidence(vol, volume) == pytest.approx(expected)


def test_confident_from_a_clarity_of_one_half_given_the_cio_cut_at_0_70():
    assert transitional_confidence(0.5, 0.5) == pytest.approx(0.7)
    assert transitional_confidence(0.49, 1.0) < 0.7


# --- the classifier ------------------------------------------------------------------------------


def classifier(volatility, volume, history=None, trend=None):
    db = MagicMock()
    adapter = MagicMock()

    async def query_latest(collection, symbol=None, limit=1):
        if collection.endswith("_volatility"):
            return [{"annualized_volatility": volatility}]
        if collection.endswith("_volume"):
            return [{"volume_spike_ratio": volume}]
        if collection.endswith("_trend"):
            return [{"rate_of_change": trend}] if trend is not None else []
        return []

    async def find_filtered(collection, filters=None, limit=100, **_kwargs):
        if collection.endswith("_regime"):
            return [
                row
                for row in (history or [])
                if all(
                    key == "symbol" or row.get(key) == value
                    for key, value in (filters or {}).items()
                )
            ][:limit]
        return []

    adapter.query_latest = AsyncMock(side_effect=query_latest)
    adapter.find_filtered = AsyncMock(side_effect=find_filtered)
    adapter.write = AsyncMock()
    db.mongodb_adapter = adapter
    return RegimeClassifier(db), adapter


@pytest.mark.asyncio
async def test_a_transitional_reading_gets_a_confidence_from_its_inputs_not_a_constant():
    near, _ = classifier(0.8, 1.1)  # high vol, medium volume: transitional
    far, _ = classifier(0.52, 1.45)  # both within a hair of a threshold
    a = await near.classify_regime("BTCUSDT", "1h")
    b = await far.classify_regime("BTCUSDT", "1h")
    assert a.regime == "transitional" and b.regime == "transitional"
    assert a.confidence == Decimal("0.74")
    assert b.confidence < Decimal("0.6") and b.confidence != Decimal("0.6")


@pytest.mark.asyncio
async def test_a_clear_transitional_reading_is_confident():
    c, _ = classifier(
        0.35, 3.0
    )  # medium vol (clarity 1), volume far above (capped 1): 0.9
    regime = await c.classify_regime("BTCUSDT", "1h")
    assert regime.regime == "transitional"
    assert regime.confidence == Decimal("0.9")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("volatility", "volume", "trend", "name", "confidence"),
    [
        (0.8, 2.0, 2.1, "bullish_acceleration", "0.85"),
        (0.8, 2.0, -2.1, "bearish_acceleration", "0.85"),
        (0.8, 2.0, 0.0, "breakout_phase", "0.9"),
        (0.8, 0.5, None, "turbulent_illiquidity", "0.8"),
        (0.1, 2.0, None, "stable_accumulation", "0.85"),
        (0.1, 0.5, None, "consolidation", "0.75"),
        (0.35, 1.0, None, "balanced_market", "0.7"),
        (0.8, 1.1, None, "transitional", "0.74"),
    ],
)
async def test_reachable_regimes_include_directional_high_high_cases(
    volatility, volume, trend, name, confidence
):
    c, _ = classifier(volatility, volume, trend=trend)
    regime = await c.classify_regime("BTCUSDT", "1h")
    assert regime.regime == name
    assert regime.confidence == Decimal(confidence)


@pytest.mark.asyncio
async def test_the_other_regimes_keep_their_rule_confidence():
    for volatility, volume, name, confidence in (
        (0.8, 0.5, "turbulent_illiquidity", "0.8"),
        (0.1, 2.0, "stable_accumulation", "0.85"),
        (0.8, 2.0, "breakout_phase", "0.9"),
        (0.1, 0.5, "consolidation", "0.75"),
        (0.35, 1.0, "balanced_market", "0.7"),
    ):
        c, _ = classifier(volatility, volume)
        regime = await c.classify_regime("BTCUSDT", "1h")
        assert regime.regime == name
        assert regime.confidence == Decimal(confidence)


@pytest.mark.asyncio
async def test_the_regime_carries_its_inputs_thresholds_and_clarity():
    c, adapter = classifier(0.8, 1.1, trend=1.5)
    regime = await c.classify_regime("BTCUSDT", "1h")
    inputs = regime.inputs
    assert inputs["annualized_volatility"] == 0.8
    assert inputs["volume_spike_ratio"] == 1.1
    assert inputs["rate_of_change"] == 1.5
    assert inputs["thresholds"] == {
        "vol_high": 0.5,
        "vol_low": 0.2,
        "volume_high": 1.5,
        "volume_low": 0.7,
    }
    assert inputs["clarity"]["volatility"] == pytest.approx(0.6)
    assert inputs["clarity"]["volume"] == pytest.approx(1.0)
    assert "min(clarity)" in inputs["confidence_basis"]
    stored = adapter.write.await_args.args[0][0]
    assert stored.inputs == inputs


@pytest.mark.asyncio
async def test_how_long_the_regime_has_held_comes_from_the_stored_history():
    now = datetime.now(UTC)
    history = [
        {
            "timeframe": "1h",
            "regime": "transitional",
            "timestamp": now - timedelta(hours=1),
        },
        {
            "timeframe": "1h",
            "regime": "transitional",
            "timestamp": now - timedelta(hours=2),
        },
        {
            "timeframe": "1h",
            "regime": "balanced_market",
            "timestamp": now - timedelta(hours=3),
        },
        {
            "timeframe": "1h",
            "regime": "transitional",
            "timestamp": now - timedelta(hours=4),
        },
    ]
    c, _ = classifier(0.8, 1.1, history=history)
    regime = await c.classify_regime("BTCUSDT", "1h")
    assert regime.inputs["held_observations"] == 3  # this one and the two before it
    assert regime.inputs["held_since"] == (now - timedelta(hours=2)).isoformat()


@pytest.mark.asyncio
async def test_held_history_does_not_mix_timeframes():
    now = datetime.now(UTC)
    history = [
        {
            "timeframe": "1h",
            "regime": "transitional",
            "timestamp": now - timedelta(hours=1),
        },
        {
            "timeframe": "4h",
            "regime": "transitional",
            "timestamp": now - timedelta(hours=2),
        },
        {
            "timeframe": "1h",
            "regime": "transitional",
            "timestamp": now - timedelta(hours=3),
        },
    ]
    c, _ = classifier(0.8, 1.1, history=history)

    regime = await c.classify_regime("BTCUSDT", "1h")

    assert regime.inputs["held_observations"] == 3
    assert regime.inputs["held_since"] == (now - timedelta(hours=3)).isoformat()


@pytest.mark.asyncio
async def test_held_history_limits_after_filtering_to_the_requested_timeframe():
    now = datetime.now(UTC)
    history = []
    for index in range(120):
        history.append(
            {
                "timeframe": "1h",
                "regime": "balanced_market" if index == 96 else "transitional",
                "timestamp": now - timedelta(hours=index * 2),
            }
        )
        history.append(
            {
                "timeframe": "4h",
                "regime": "transitional",
                "timestamp": now - timedelta(hours=index * 2 + 1),
            }
        )

    c, adapter = classifier(0.8, 1.1, history=history)
    regime = await c.classify_regime("BTCUSDT", "1h")

    assert regime.inputs["held_observations"] == 97  # current plus latest 96 1h rows
    assert regime.inputs["held_since"] == (now - timedelta(hours=95 * 2)).isoformat()
    adapter.find_filtered.assert_awaited_once_with(
        "analytics_BTCUSDT_regime",
        filters={"symbol": "BTCUSDT", "timeframe": "1h"},
        limit=96,
    )


@pytest.mark.asyncio
async def test_a_history_failure_does_not_fail_the_classification():
    c, adapter = classifier(0.8, 1.1)
    adapter.find_filtered = AsyncMock(side_effect=RuntimeError("history unavailable"))
    regime = await c.classify_regime("BTCUSDT", "1h")
    assert regime is not None
    assert regime.inputs["held_observations"] == 1


# --- the route -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_route_carries_the_inputs_and_stays_backward_compatible():
    from data_manager.api.routes.analysis import get_regime

    base = {
        "regime": "transitional",
        "volatility_level": "high",
        "volume_level": "medium",
        "trend_direction": "neutral",
        "confidence": "0.74",
        "metadata": {"computed_at": datetime(2026, 10, 1, tzinfo=UTC)},
    }
    inputs = {"annualized_volatility": 0.8, "held_observations": 3}
    for row, expected in ((base, None), ({**base, "inputs": inputs}, inputs)):
        manager = MagicMock()
        manager.mongodb_adapter.query_latest = AsyncMock(return_value=[row])
        with patch("data_manager.api.routes.analysis.api_module") as api:
            api.db_manager = manager
            result = await get_regime(pair="BTCUSDT", period="1h")
        data = result["data"]
        assert data["regime"] == "transitional" and data["confidence"] == "0.74"
        assert data["volatility_level"] == "high" and data["volume_level"] == "medium"
        assert data["inputs"] == expected
