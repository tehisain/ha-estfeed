"""Tests for Estfeed sensor entities."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.estfeed.api import AccountingInterval
from custom_components.estfeed.const import Kind
from custom_components.estfeed.sensor import (
    LaggingPeriod,
    sum_for_period,
    window_for_period,
)

TALLINN = ZoneInfo("Europe/Tallinn")


def _ival(period_start: datetime, kwh: float) -> AccountingInterval:
    return AccountingInterval(
        period_start=period_start,
        consumption_kwh=kwh,
        production_kwh=None,
        consumption_m3=None,
        production_m3=None,
    )


def test_window_yesterday_in_local_tz():
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.YESTERDAY, now=now, tz=TALLINN)
    assert start == datetime(2026, 4, 28, 0, tzinfo=TALLINN)
    assert end == datetime(2026, 4, 29, 0, tzinfo=TALLINN)


def test_window_month_to_date_in_local_tz():
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.MONTH_TO_DATE, now=now, tz=TALLINN)
    assert start == datetime(2026, 4, 1, 0, tzinfo=TALLINN)
    assert end == now


def test_window_previous_month_in_local_tz():
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.PREVIOUS_MONTH, now=now, tz=TALLINN)
    assert start == datetime(2026, 3, 1, 0, tzinfo=TALLINN)
    assert end == datetime(2026, 4, 1, 0, tzinfo=TALLINN)


def test_window_previous_month_january_rolls_to_december():
    now = datetime(2026, 1, 5, 12, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.PREVIOUS_MONTH, now=now, tz=TALLINN)
    assert start == datetime(2025, 12, 1, 0, tzinfo=TALLINN)
    assert end == datetime(2026, 1, 1, 0, tzinfo=TALLINN)


def test_sum_for_period_yesterday():
    intervals = [_ival(datetime(2026, 4, 27, h, tzinfo=UTC), 1.0) for h in range(24)] + [
        _ival(datetime(2026, 4, 28, h, tzinfo=UTC), 2.0) for h in range(24)
    ]
    now = datetime(2026, 4, 29, 12, tzinfo=TALLINN)
    total = sum_for_period(
        intervals, Kind.CONSUMPTION, LaggingPeriod.YESTERDAY, now=now, tz=TALLINN
    )
    # Yesterday in Tallinn = 2026-04-28 local = 2026-04-27 21:00Z .. 2026-04-28 21:00Z
    # That spans 3 hours of the 27th-utc bucket (21,22,23) and 21 hours of the 28th-utc.
    expected = 3 * 1.0 + 21 * 2.0
    assert total == pytest.approx(expected)


def test_sum_for_period_dst_spring_forward_23h_day():
    """Last Sunday of March in Europe/Tallinn: 03:00 EET → 04:00 EEST = 23-hour day."""
    # 2026-03-29 is the spring-forward day in EU.
    # We compute YESTERDAY at 2026-03-30 noon local — that yesterday is 23 hours long.
    intervals = [
        _ival(datetime(2026, 3, 28, 22, tzinfo=UTC), 1.0),  # 2026-03-29 00:00 EET
        _ival(datetime(2026, 3, 28, 23, tzinfo=UTC), 1.0),  # 2026-03-29 01:00 EET
        _ival(datetime(2026, 3, 29, 0, tzinfo=UTC), 1.0),  # 2026-03-29 02:00 EET
        # 03:00 EET does not exist; clock jumps to 04:00 EEST (= 01:00 UTC)
        _ival(datetime(2026, 3, 29, 1, tzinfo=UTC), 1.0),  # 2026-03-29 04:00 EEST
        # ... continuing through 2026-03-29 23:00 EEST (= 20:00 UTC)
        *[_ival(datetime(2026, 3, 29, h, tzinfo=UTC), 1.0) for h in range(2, 21)],
    ]
    now = datetime(2026, 3, 30, 12, tzinfo=TALLINN)
    total = sum_for_period(
        intervals, Kind.CONSUMPTION, LaggingPeriod.YESTERDAY, now=now, tz=TALLINN
    )
    # 23 intervals fall in the spring-forward day
    assert total == pytest.approx(23.0)
