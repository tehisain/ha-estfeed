"""Tests for EstfeedCoordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.estfeed.api import (
    AccountingInterval,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_BACKFILL_MONTHS,
    CONF_RESOLUTION,
    CommodityType,
    Resolution,
)
from custom_components.estfeed.coordinator import EstfeedCoordinator


def _make_meter(eic: str = "38ZEE-00720089-N") -> MeteringPoint:
    return MeteringPoint(
        eic=eic,
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


def _hourly(start: datetime, hours: int, kwh: float = 0.5) -> list[AccountingInterval]:
    return [
        AccountingInterval(
            period_start=start + timedelta(hours=h),
            consumption_kwh=kwh,
            production_kwh=0.0,
            consumption_m3=None,
            production_m3=None,
        )
        for h in range(hours)
    ]


@pytest.mark.asyncio
async def test_coordinator_first_update_fetches_and_writes(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=_hourly(datetime(2026, 4, 28, 0, tzinfo=UTC), 24),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ) as mock_write,
    ):
        await coordinator._async_update_data()

    # One call per (eic, kind) pair = 2 (consumption + production)
    assert mock_write.call_count == 2


@pytest.mark.asyncio
async def test_coordinator_uses_latest_seen_as_start(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[])]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    last_seen_ts = datetime(2026, 4, 28, 23, tzinfo=UTC).timestamp()
    fake_last_stats = {
        "estfeed:home_consumption_089N": [{"end": last_seen_ts * 1000}],  # ms
    }

    with (
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=AsyncMock(return_value=fake_last_stats),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator._async_update_data()

    # First call should request from latest_seen + 1 hour onwards.
    # Our get_metering_data signature is (start, end, resolution, eics=...) so
    # args[0] is the start datetime.
    args, _ = client.get_metering_data.call_args
    assert args[0] == datetime(2026, 4, 29, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_coordinator_per_meter_error_is_skipped(hass):
    """A meter returning an `error` field is skipped without crashing the tick."""
    from custom_components.estfeed.api import MeterError

    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=[],
                error=MeterError(id="x", message="m", code="c", trace_id="t", args=[]),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ) as mock_write,
    ):
        await coordinator._async_update_data()

    mock_write.assert_not_called()
