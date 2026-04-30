"""Tests for the Estfeed API client and data types."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.estfeed.api import (
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import CommodityType


def test_metering_point_from_dict():
    raw = {
        "eic": "38ZEE-00720089-N",
        "commodityType": "ELECTRICITY",
        "periods": [{"from": "2019-07-27T21:00:00Z"}],
    }
    mp = MeteringPoint.from_dict(raw)
    assert mp.eic == "38ZEE-00720089-N"
    assert mp.commodity_type == CommodityType.ELECTRICITY
    assert mp.periods == [Period(start=datetime(2019, 7, 27, 21, 0, tzinfo=UTC), end=None)]


def test_metering_point_from_dict_with_period_end():
    raw = {
        "eic": "X",
        "commodityType": "NATURAL_GAS",
        "periods": [{"from": "2020-01-01T00:00:00Z", "to": "2024-01-01T00:00:00Z"}],
    }
    mp = MeteringPoint.from_dict(raw)
    assert mp.periods[0].end == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def test_meter_data_from_dict():
    raw = {
        "meteringPointEic": "38ZEE-00720089-N",
        "accountingIntervals": [
            {"periodStart": "2026-04-27T00:00:00Z", "consumptionKwh": 0.348, "productionKwh": 0.0},
            {"periodStart": "2026-04-27T01:00:00Z", "consumptionKwh": 0.338, "productionKwh": 0.0},
        ],
    }
    md = MeterData.from_dict(raw)
    assert md.eic == "38ZEE-00720089-N"
    assert md.error is None
    assert len(md.intervals) == 2
    assert md.intervals[0].period_start == datetime(2026, 4, 27, 0, 0, tzinfo=UTC)
    assert md.intervals[0].consumption_kwh == 0.348
    assert md.intervals[0].production_kwh == 0.0


def test_meter_data_with_per_meter_error():
    raw = {
        "meteringPointEic": "X",
        "accountingIntervals": [],
        "error": {
            "id": "abc",
            "message": "boom",
            "code": "error.boom",
            "traceId": "t1",
            "args": [],
        },
    }
    md = MeterData.from_dict(raw)
    assert md.error is not None
    assert md.error.code == "error.boom"
