"""Tests for the data_fresh binary sensor."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from freezegun import freeze_time

from custom_components.estfeed.api import (
    AccountingInterval,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.binary_sensor import DataFreshBinarySensor
from custom_components.estfeed.const import CommodityType, Kind


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


def _ival(t: datetime) -> AccountingInterval:
    return AccountingInterval(
        period_start=t,
        consumption_kwh=1.0,
        production_kwh=None,
        consumption_m3=None,
        production_m3=None,
    )


@freeze_time("2026-04-29T12:00:00Z")
def test_data_fresh_when_recent():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [_ival(datetime(2026, 4, 28, 23, tzinfo=UTC))]
    }
    coordinator.last_update_success = True
    coordinator.slug = "home"
    sensor = DataFreshBinarySensor(coordinator=coordinator, meter=_meter())
    assert sensor.is_on is True


@freeze_time("2026-04-29T12:00:00Z")
def test_data_stale_when_older_than_threshold():
    coordinator = MagicMock()
    coordinator.cache = {
        ("38ZEE-00720089-N", Kind.CONSUMPTION): [_ival(datetime(2026, 4, 28, 0, tzinfo=UTC))]
    }
    coordinator.last_update_success = True
    coordinator.slug = "home"
    sensor = DataFreshBinarySensor(coordinator=coordinator, meter=_meter())
    # 2026-04-28 00:00 UTC is 36 hours before 2026-04-29 12:00 UTC → stale
    assert sensor.is_on is False


def test_data_fresh_unknown_when_cache_empty():
    coordinator = MagicMock()
    coordinator.cache = {}
    coordinator.last_update_success = True
    coordinator.slug = "home"
    sensor = DataFreshBinarySensor(coordinator=coordinator, meter=_meter())
    assert sensor.is_on is None
