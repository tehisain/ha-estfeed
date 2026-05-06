"""Tests for statistics helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

from custom_components.estfeed.api import AccountingInterval
from custom_components.estfeed.const import Kind
from custom_components.estfeed.statistics import (
    StatisticStream,
    async_write_meter_statistics,
    build_statistic_id,
    compute_statistic_rows,
    eic_suffix,
)


def test_eic_suffix():
    assert eic_suffix("38ZEE-00720089-N") == "089n"
    assert eic_suffix("XYZW-12345678-AB") == "78ab"


def test_statistic_id_matches_ha_recorder_regex():
    """HA recorder validates statistic_id against ^[a-z0-9_]+:[a-z0-9_]+$.
    Capital letters in the EIC suffix would fail validation."""
    import re

    sid = build_statistic_id(
        "home", Kind.CONSUMPTION, eic_suffix("38ZEE-00720089-N"), multi_meter=False
    )
    assert re.fullmatch(r"[a-z0-9_]+:[a-z0-9_]+", sid), sid


def test_build_statistic_id_single_meter():
    assert (
        build_statistic_id("home", Kind.CONSUMPTION, "089n", multi_meter=False)
        == "estfeed:home_consumption_089n"
    )


def test_build_statistic_id_multi_meter():
    # When multiple meters share an entry, suffix is appended even if slug carries it.
    assert (
        build_statistic_id("home", Kind.PRODUCTION, "089n", multi_meter=True)
        == "estfeed:home_production_089n"
    )


def test_compute_statistic_rows_first_chunk():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, h, tzinfo=UTC),
            consumption_kwh=1.0 + h * 0.1,
            production_kwh=0.0,
            consumption_m3=None,
            production_m3=None,
        )
        for h in range(3)
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert len(rows) == 3
    assert rows[0]["start"] == datetime(2026, 4, 27, 0, tzinfo=UTC)
    assert rows[0]["sum"] == 1.0
    assert rows[1]["sum"] == 2.1  # 1.0 + 1.1
    assert rows[2]["sum"] == 3.3  # 2.1 + 1.2
    # state mirrors sum for counter semantics
    assert rows[0]["state"] == rows[0]["sum"]


def test_compute_statistic_rows_continuation_with_prior_sum():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 28, 0, tzinfo=UTC),
            consumption_kwh=2.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=100.0)
    assert rows[0]["sum"] == 102.0


def test_compute_statistic_rows_skips_none_values():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert rows == []


def test_compute_statistic_rows_sorts_by_start():
    """Rows must be ascending by start regardless of input order."""
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 2, tzinfo=UTC),
            consumption_kwh=0.5,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=1.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 1, tzinfo=UTC),
            consumption_kwh=2.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)
    sums = [r["sum"] for r in rows]
    assert sums == [1.0, 3.0, 3.5]


@pytest.mark.asyncio
async def test_async_write_meter_statistics_calls_external_stats(hass):
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=1.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    stream = StatisticStream(
        statistic_id="estfeed:home_consumption_089n",
        name="Home consumption (38ZEE-00720089-N)",
        unit="kWh",
        kind=Kind.CONSUMPTION,
    )
    with patch(
        "custom_components.estfeed.statistics.async_add_external_statistics",
        new=Mock(),
    ) as mock_add:
        result = await async_write_meter_statistics(hass, stream, intervals, prior_sum=0.0)

    mock_add.assert_called_once()
    metadata, rows = mock_add.call_args.args[1], mock_add.call_args.args[2]
    assert metadata["statistic_id"] == "estfeed:home_consumption_089n"
    assert metadata["source"] == "estfeed"
    assert metadata["unit_of_measurement"] == "kWh"
    assert metadata["has_sum"] is True
    assert metadata["has_mean"] is False
    # unit_class becomes mandatory in HA 2026.11; kWh maps to energy.
    assert metadata["unit_class"] == "energy"
    assert len(rows) == 1
    assert rows[0]["sum"] == 1.0
    # Returns final running sum so multi-chunk callers can chain prior_sum.
    assert result == 1.0


@pytest.mark.asyncio
async def test_async_write_meter_statistics_sets_volume_unit_class_for_gas(hass):
    """m³ (gas meters) must declare unit_class=volume so HA's recorder
    routes the stream through VolumeConverter, not silently drop it after
    the 2026.11 cutover."""
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=2.0,
            production_m3=None,
        )
    ]
    stream = StatisticStream(
        statistic_id="estfeed:home_consumption_089n",
        name="Home consumption (gas)",
        unit="m³",
        kind=Kind.CONSUMPTION,
    )
    with patch(
        "custom_components.estfeed.statistics.async_add_external_statistics",
        new=Mock(),
    ) as mock_add:
        await async_write_meter_statistics(hass, stream, intervals, prior_sum=0.0)

    metadata = mock_add.call_args.args[1]
    assert metadata["unit_of_measurement"] == "m³"
    assert metadata["unit_class"] == "volume"


@pytest.mark.asyncio
async def test_async_write_meter_statistics_noop_when_no_rows(hass):
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    stream = StatisticStream(
        statistic_id="estfeed:home_consumption_089n",
        name="x",
        unit="kWh",
        kind=Kind.CONSUMPTION,
    )
    with patch(
        "custom_components.estfeed.statistics.async_add_external_statistics",
        new=Mock(),
    ) as mock_add:
        result = await async_write_meter_statistics(hass, stream, intervals, prior_sum=42.0)
    mock_add.assert_not_called()
    # No rows produced → return prior_sum unchanged for the chunk loop to chain.
    assert result == 42.0


def test_compute_statistic_rows_snaps_to_top_of_hour():
    """Regression: Estfeed API can return intervals at HH:32:13.598 if the
    request start was non-aligned. HA's recorder requires top-of-hour
    timestamps, so rows must be snapped down."""
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 5, 2, 10, 32, 13, 598481, tzinfo=UTC),
            consumption_kwh=0.35,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 5, 2, 11, 32, 13, 598481, tzinfo=UTC),
            consumption_kwh=0.348,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    starts = [r["start"] for r in rows]
    assert starts == [
        datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        datetime(2026, 5, 2, 11, 0, tzinfo=UTC),
    ]
    for s in starts:
        assert s.minute == 0 and s.second == 0 and s.microsecond == 0


def test_compute_statistic_rows_aggregates_subhour_intervals():
    """Quarter-hour intervals in the same hour are summed into one bucket."""
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
            consumption_kwh=0.1,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 5, 2, 10, 15, tzinfo=UTC),
            consumption_kwh=0.2,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 5, 2, 10, 30, tzinfo=UTC),
            consumption_kwh=0.3,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 5, 2, 10, 45, tzinfo=UTC),
            consumption_kwh=0.4,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert len(rows) == 1
    assert rows[0]["start"] == datetime(2026, 5, 2, 10, 0, tzinfo=UTC)
    assert rows[0]["sum"] == pytest.approx(1.0)
