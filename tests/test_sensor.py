"""Tests for Estfeed sensor entities."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy

from custom_components.estfeed.api import AccountingInterval, MeteringPoint, Period
from custom_components.estfeed.const import CommodityType, Kind
from custom_components.estfeed.coordinator import CumulativeBaseline
from custom_components.estfeed.sensor import (
    CumulativeSinceResetSensor,
    LaggingPeriod,
    LaggingSensor,
    LatestIntervalSensor,
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


def test_window_today_in_local_tz():
    now = datetime(2026, 4, 29, 12, 30, tzinfo=TALLINN)
    start, end = window_for_period(LaggingPeriod.TODAY, now=now, tz=TALLINN)
    assert start == datetime(2026, 4, 29, 0, tzinfo=TALLINN)
    assert end == now


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


def test_sum_for_period_today_partial_window():
    """TODAY should sum only the hours that have already elapsed today, not the
    full 24h. The window end is `now`, not midnight, so a meter that hasn't
    reported the rest of today yet still gives a sensible running total."""
    intervals = [_ival(datetime(2026, 4, 28, 21, tzinfo=UTC), 0.5)] + [
        _ival(datetime(2026, 4, 29, h, tzinfo=UTC), 1.0) for h in range(6)
    ]
    # 12:30 local = 09:30 UTC. Hours 0..5 UTC = 03:00..08:00 local — all today.
    now = datetime(2026, 4, 29, 12, 30, tzinfo=TALLINN)
    total = sum_for_period(intervals, Kind.CONSUMPTION, LaggingPeriod.TODAY, now=now, tz=TALLINN)
    # Today in Tallinn = 2026-04-29 local = 2026-04-28 21:00Z .. 09:30Z today.
    # That includes the 0.5 sample at 21:00Z + six 1.0 samples at 00..05Z.
    assert total == pytest.approx(6.5)


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


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


def test_lagging_sensor_state_with_data():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [
            _ival(datetime(2026, 4, 28, h, tzinfo=UTC), 1.0) for h in range(24)
        ]
    }
    coordinator.hass.config.time_zone = "Europe/Tallinn"
    coordinator.slug = "home"
    coordinator.last_meter_errors = {}

    sensor = LaggingSensor(
        coordinator=coordinator,
        meter=_meter(),
        kind=Kind.CONSUMPTION,
        period=LaggingPeriod.YESTERDAY,
        multi_meter=False,
    )

    assert sensor.unique_id == "estfeed_home_consumption_yesterday_089n"
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR


def test_lagging_sensor_unavailable_when_cache_empty():
    coordinator = MagicMock()
    coordinator.cache = {}
    coordinator.hass.config.time_zone = "Europe/Tallinn"
    coordinator.slug = "home"
    coordinator.last_meter_errors = {}
    coordinator.last_update_success = True

    sensor = LaggingSensor(
        coordinator=coordinator,
        meter=_meter(),
        kind=Kind.CONSUMPTION,
        period=LaggingPeriod.YESTERDAY,
        multi_meter=False,
    )
    assert sensor.available is False


def test_cumulative_sensor_delegates_to_coordinator():
    """The sensor's value comes straight from
    ``coordinator.cumulative_since_reset`` so the cache-based computation is
    exercised end-to-end. A regression that re-introduced the legacy
    ``latest_sum - baseline.sum`` formula (which inherited recorder-cumulative
    corruption and showed +6490 kWh against a real ~12 kWh) would fail here."""
    coordinator = MagicMock()
    coordinator.slug = "home"
    coordinator.baselines = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): CumulativeBaseline(
            reset_at=datetime(2026, 5, 18, 12, tzinfo=UTC)
        )
    }
    coordinator.cumulative_since_reset.return_value = 12.345

    sensor = CumulativeSinceResetSensor(coordinator, _meter(), Kind.CONSUMPTION)

    assert sensor.available is True
    assert sensor.native_value == 12.345
    coordinator.cumulative_since_reset.assert_called_with("38ZEE-00720089-N", Kind.CONSUMPTION)
    assert sensor.last_reset == datetime(2026, 5, 18, 12, tzinfo=UTC)
    assert sensor.unique_id == "estfeed_home_consumption_cumulative_089n"


def test_cumulative_sensor_unavailable_before_baseline_exists():
    """No baseline yet → sensor is unavailable. This is the state between
    setup and the first ``async_ensure_baselines`` call."""
    coordinator = MagicMock()
    coordinator.baselines = {}
    coordinator.cumulative_since_reset.return_value = None
    coordinator.slug = "home"

    sensor = CumulativeSinceResetSensor(coordinator, _meter(), Kind.CONSUMPTION)
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.last_reset is None


def test_cumulative_sensor_reads_zero_immediately_after_reset():
    """Right after the user presses reset, no cache intervals are past
    reset_at — coordinator returns 0.0 and the sensor surfaces that."""
    coordinator = MagicMock()
    key = ("38ZEE-00720089-N", Kind.CONSUMPTION)
    coordinator.baselines = {key: CumulativeBaseline(reset_at=datetime(2026, 5, 18, tzinfo=UTC))}
    coordinator.cumulative_since_reset.return_value = 0.0
    coordinator.slug = "home"

    sensor = CumulativeSinceResetSensor(coordinator, _meter(), Kind.CONSUMPTION)
    assert sensor.native_value == 0.0


def test_latest_interval_sensor_returns_max_period_start():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [
            _ival(datetime(2026, 4, 28, h, tzinfo=UTC), 1.0) for h in range(3)
        ]
    }
    coordinator.last_update_success = True
    coordinator.slug = "home"

    sensor = LatestIntervalSensor(coordinator=coordinator, meter=_meter())
    assert sensor.native_value == datetime(2026, 4, 28, 2, tzinfo=UTC)
