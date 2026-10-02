from datetime import UTC, date, datetime, timedelta

from data_manager.maintenance.historic_copy_proof import prove_daily_copy


def test_daily_copy_proof_requires_mysql_to_cover_each_day():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    day = date(2026, 10, 8)
    result = prove_daily_copy(
        "trades",
        {day: 5},
        {day: 4},
        now=now,
        retention_days=1,
        copy_lag=timedelta(days=2),
    )

    assert result.proven is False
    assert result.failures == ("2026-10-08: mongo=5 mysql=4",)


def test_daily_copy_proof_passes_when_mysql_is_complete():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    day = date(2026, 10, 8)
    result = prove_daily_copy(
        "execution_events",
        {day: 4},
        {day: 4},
        now=now,
        retention_days=1,
        copy_lag=timedelta(days=2),
    )

    assert result.proven is True
