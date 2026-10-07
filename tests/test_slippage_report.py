"""Slippage per regime from the per-fill cost telemetry (dm#535)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from data_manager.maintenance import slippage_report
from data_manager.services.slippage_report import (
    NO_REGIME,
    RegimeTimeline,
    build_report,
    fill_role,
    percentile,
    summary_lines,
)

T0 = datetime(2026, 10, 6, 12, tzinfo=UTC)


def _fill(symbol, slippage, minutes, **extra):
    row = {
        "event_type": "filled",
        "symbol": symbol,
        "fill_time": T0 + timedelta(minutes=minutes),
        "payload": {"slippage_bp": slippage, **extra.pop("payload", {})},
    }
    row.update(extra)
    return row


def _regime(regime, minutes, **extra):
    return {
        "regime": regime,
        "metadata": {"computed_at": T0 + timedelta(minutes=minutes)},
        **extra,
    }


def test_the_regime_in_force_is_the_newest_one_computed_at_or_before_the_fill():
    timeline = RegimeTimeline(
        [
            _regime("balanced_market", 60),
            _regime("turbulent_illiquidity", 0),
            _regime("breakout_phase", 120),
        ]
    )

    assert timeline.at(T0 - timedelta(minutes=1)) == NO_REGIME
    assert timeline.at(T0) == "turbulent_illiquidity"  # at the computed time itself
    assert timeline.at(T0 + timedelta(minutes=59)) == "turbulent_illiquidity"
    assert timeline.at(T0 + timedelta(minutes=60)) == "balanced_market"
    assert timeline.at(T0 + timedelta(minutes=500)) == "breakout_phase"


def test_regime_documents_may_carry_the_time_at_the_top_level_or_in_metadata():
    timeline = RegimeTimeline(
        [
            {"regime": "a", "computed_at": T0},
            {"regime": "b", "timestamp": T0 + timedelta(minutes=10)},
            {"regime": "no time"},  # unusable: skipped
            {
                "metadata": {"computed_at": T0 + timedelta(minutes=20)}
            },  # no regime: skipped
        ]
    )

    assert timeline.at(T0 + timedelta(minutes=5)) == "a"
    assert timeline.at(T0 + timedelta(minutes=15)) == "b"
    assert timeline.at(T0 + timedelta(minutes=25)) == "b"


def test_percentiles_interpolate():
    assert percentile([5.0], 90) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert percentile([0.0, 10.0], 90) == pytest.approx(9.0)
    assert percentile([4.0, 1.0, 3.0, 2.0], 90) == pytest.approx(3.7)


def test_slippage_is_grouped_by_regime_with_count_mean_median_p90_and_the_ratio():
    regimes = {
        "ADAUSDT": [_regime("turbulent_illiquidity", 0)],
        "ETHUSDT": [_regime("balanced_market", 0)],
    }
    fills = [
        _fill("ADAUSDT", 4.0, 10),
        _fill("ADAUSDT", 6.0, 20),
        _fill("ADAUSDT", 8.0, 30),
        _fill("ETHUSDT", 1.0, 10),
        _fill("ETHUSDT", 2.0, 20),
        _fill("ETHUSDT", 3.0, 30),
    ]

    report = build_report(fills, regimes)

    assert report["overall"]["count"] == 6
    assert report["overall"]["median_bp"] == pytest.approx(3.5)
    turbulent = report["by_regime"]["turbulent_illiquidity"]
    assert turbulent["count"] == 3
    assert turbulent["mean_bp"] == pytest.approx(6.0)
    assert turbulent["median_bp"] == pytest.approx(6.0)
    assert turbulent["p90_bp"] == pytest.approx(7.6)
    assert turbulent["ratio_to_overall_median"] == pytest.approx(6.0 / 3.5)
    assert report["by_regime"]["balanced_market"][
        "ratio_to_overall_median"
    ] == pytest.approx(2.0 / 3.5)
    assert report["by_regime_and_symbol"]["turbulent_illiquidity/ADAUSDT"]["count"] == 3


def test_a_regime_is_split_by_symbol():
    regimes = {s: [_regime("transitional", 0)] for s in ("ADAUSDT", "LINKUSDT")}
    fills = [_fill("ADAUSDT", 2.0, 5), _fill("LINKUSDT", 10.0, 5)]

    report = build_report(fills, regimes)

    assert report["by_regime"]["transitional"]["count"] == 2
    assert report["by_regime_and_symbol"]["transitional/ADAUSDT"]["median_bp"] == 2.0
    assert report["by_regime_and_symbol"]["transitional/LINKUSDT"]["median_bp"] == 10.0


def test_a_fill_with_no_regime_available_is_reported_not_dropped():
    regimes = {"ETHUSDT": [_regime("balanced_market", 100)]}
    fills = [
        _fill("ETHUSDT", 3.0, 10),
        _fill("XRPUSDT", 5.0, 10),
    ]  # before the first regime; no regimes at all

    report = build_report(fills, regimes)

    assert report["by_regime"][NO_REGIME]["count"] == 2
    assert report["by_regime_and_symbol"][f"{NO_REGIME}/ETHUSDT"]["count"] == 1
    assert report["by_regime_and_symbol"][f"{NO_REGIME}/XRPUSDT"]["count"] == 1


def test_fills_without_slippage_are_counted_and_left_out_of_the_statistics():
    fills = [
        _fill("ETHUSDT", 2.0, 5),
        {"event_type": "filled", "symbol": "ETHUSDT", "fill_time": T0, "payload": {}},
        _fill("ETHUSDT", None, 6),
        {"event_type": "placed", "symbol": "ETHUSDT"},  # not a fill
    ]

    report = build_report(fills, {"ETHUSDT": [_regime("balanced_market", 0)]})

    assert report["fills_considered"] == 3
    assert report["fills_with_slippage"] == 1
    assert report["fills_without_slippage"] == 2
    assert report["by_regime"]["balanced_market"]["count"] == 1


def test_telemetry_may_be_on_the_event_or_in_its_payload():
    nested = _fill("ETHUSDT", 2.0, 5)
    top_level = {
        "event_type": "filled",
        "symbol": "ETHUSDT",
        "fill_time": T0 + timedelta(minutes=6),
        "slippage_bp": 4.0,
    }

    report = build_report(
        [nested, top_level], {"ETHUSDT": [_regime("balanced_market", 0)]}
    )

    assert report["fills_with_slippage"] == 2
    assert report["overall"]["mean_bp"] == pytest.approx(3.0)


def test_entry_and_exit_fills_can_be_separated_by_role():
    entry = _fill("ETHUSDT", 1.0, 5)
    exit_fill = _fill("ETHUSDT", 9.0, 6, payload={"reduce_only": True})
    explicit = _fill("ETHUSDT", 7.0, 7, payload={"role": "entry", "reduce_only": True})
    regimes = {"ETHUSDT": [_regime("balanced_market", 0)]}

    assert fill_role(entry) == "entry"
    assert fill_role(exit_fill) == "exit"
    assert fill_role(explicit) == "entry"  # an explicit role wins
    assert (
        build_report([entry, exit_fill, explicit], regimes, role="exit")["overall"][
            "median_bp"
        ]
        == 9.0
    )
    assert (
        build_report([entry, exit_fill, explicit], regimes, role="entry")[
            "fills_with_slippage"
        ]
        == 2
    )
    assert build_report([entry, exit_fill, explicit], regimes)["role"] == "all"


def test_an_empty_report_has_no_statistics_and_no_ratio_without_an_overall_median():
    report = build_report([], {})

    assert report["overall"] is None
    assert report["by_regime"] == {}
    zero = build_report(
        [_fill("ETHUSDT", 0.0, 5)], {"ETHUSDT": [_regime("balanced_market", 0)]}
    )
    assert (
        zero["by_regime"]["balanced_market"]["ratio_to_overall_median"] is None
    )  # a zero overall median


def test_the_summary_has_a_line_per_regime():
    regimes = {"ADAUSDT": [_regime("turbulent_illiquidity", 0)]}
    report = build_report([_fill("ADAUSDT", 4.0, 5), _fill("ADAUSDT", 6.0, 6)], regimes)

    lines = summary_lines(report)

    assert lines[0].startswith(
        "slippage per regime (role=all): 2 of 2 fills have slippage"
    )
    assert "overall median 5.00 bp (n=2)" in lines[1]
    assert lines[2].startswith("turbulent_illiquidity: n=2 mean=5.00 median=5.00")


def _db(fills, regimes):
    def collection(name):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.to_list = AsyncMock(
            return_value=fills if name == "execution_events" else regimes.get(name, [])
        )
        found = MagicMock()
        found.find.return_value = cursor
        return found

    class Db(dict):
        def __getitem__(self, name):
            return collection(name)

    return SimpleNamespace(mongodb_adapter=SimpleNamespace(db=Db()))


@pytest.mark.asyncio
async def test_the_endpoint_joins_fills_with_the_regimes_of_their_symbols():
    import data_manager.api.app as api_module
    from data_manager.api.routes.analysis import get_slippage_by_regime

    now = datetime.now(UTC)
    fills = [
        {
            "event_type": "filled",
            "symbol": "ADAUSDT",
            "fill_time": now,
            "payload": {"slippage_bp": 8.0},
        },
        {
            "event_type": "filled",
            "symbol": "ADAUSDT",
            "fill_time": now,
            "payload": {"slippage_bp": 4.0},
        },
    ]
    regimes = {
        "analytics_ADAUSDT_regime": [
            {"regime": "turbulent_illiquidity", "computed_at": now - timedelta(hours=1)}
        ]
    }
    api_module.db_manager = _db(fills, regimes)
    try:
        report = await get_slippage_by_regime(
            window_days=7.0, symbol="ADAUSDT", role=None
        )
    finally:
        api_module.db_manager = None

    assert report["by_regime"]["turbulent_illiquidity"]["median_bp"] == 6.0
    assert report["metadata"]["fills_read"] == 2
    assert report["metadata"]["truncated"] is False


@pytest.mark.asyncio
async def test_the_endpoint_needs_the_database():
    import data_manager.api.app as api_module
    from data_manager.api.routes.analysis import get_slippage_by_regime

    api_module.db_manager = None
    with pytest.raises(HTTPException) as excinfo:
        await get_slippage_by_regime(window_days=30.0, symbol=None, role=None)

    assert excinfo.value.status_code == 503


def test_the_cli_prints_the_summary_and_the_json(monkeypatch, capsys):
    report = build_report(
        [_fill("ADAUSDT", 4.0, 5)], {"ADAUSDT": [_regime("turbulent_illiquidity", 0)]}
    )

    async def fake_run(_args):
        return report

    monkeypatch.setattr(slippage_report, "run", fake_run)

    assert slippage_report.main(["--days", "7", "--role", "entry"]) == 0
    out = capsys.readouterr().out
    assert "turbulent_illiquidity: n=1" in out
    assert '"by_regime"' in out


def test_fills_without_slippage_are_split_into_no_telemetry_and_no_intended_price():
    fills = [
        _fill("ETHUSDT", 2.0, 5),
        # emitted before the telemetry existed on this path: no slippage_bp key at all
        {"event_type": "filled", "symbol": "ETHUSDT", "fill_time": T0, "payload": {}},
        {"event_type": "filled", "symbol": "ETHUSDT", "fill_time": T0},
        # emitted, but with no intended price to measure against
        _fill("ETHUSDT", None, 6, payload={"intended_price": None}),
        # emitted with an intended price but no usable fill price
        _fill("ETHUSDT", None, 7, payload={"intended_price": 100.0}),
    ]
    report = build_report(fills, {"ETHUSDT": [_regime("balanced_market", 0)]})
    assert report["fills_considered"] == 5
    assert report["fills_with_slippage"] == 1
    assert report["fills_without_slippage"] == 4
    assert report["fills_without_cost_telemetry"] == 2
    assert report["fills_without_intended_price"] == 1
    line = summary_lines(report)[0]
    assert "2 carry no cost telemetry" in line and "1 have no intended price" in line
