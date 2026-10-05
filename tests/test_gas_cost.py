"""Gas cost pricing modes and the trailing re-price window, against HA's recorder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.estfeed.api import AccountingInterval, MeterData
from custom_components.estfeed.const import GasPriceMode
from custom_components.estfeed.coordinator import EstfeedCoordinator
from custom_components.estfeed.gas_price import gas_day_for
from tests.test_config_flow import _setup_recorder
from tests.test_coordinator import _gas_meter

COST_ID = "estfeed:home_gas_cost_099g"


def _coordinator(hass, options: dict) -> EstfeedCoordinator:
    coord = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options=options)
    coord.meters = [_gas_meter()]
    return coord


@pytest.mark.parametrize(
    ("options", "mode"),
    [
        ({}, GasPriceMode.OFF),
        # Entries saved before the mode option existed only had a fixed price.
        ({"gas_price_eur_per_kwh": 0.05}, GasPriceMode.FIXED),
        ({"gas_price_mode": "exchange"}, GasPriceMode.EXCHANGE),
        ({"gas_price_mode": "off", "gas_price_eur_per_kwh": 0.05}, GasPriceMode.OFF),
    ],
)
def test_gas_price_mode(hass, options, mode):
    coord = _coordinator(hass, options)
    assert coord.gas_price_mode == mode
    assert bool(coord.cost_streams_for(_gas_meter())) == (mode != GasPriceMode.OFF)


def _gas_day_boundary_hour() -> datetime:
    """UTC hour starting at 07:00 Tallinn two days ago: a gas-day boundary."""
    local = datetime.now(ZoneInfo("Europe/Tallinn")) - timedelta(days=2)
    return local.replace(hour=7, minute=0, second=0, microsecond=0).astimezone(UTC)


@pytest.mark.asyncio
async def test_exchange_cost_placeholders_are_repriced_when_index_is_published(hass):
    await _setup_recorder(hass)
    boundary = _gas_day_boundary_hour()
    h0, h1, h2 = boundary - timedelta(hours=1), boundary, boundary + timedelta(hours=1)
    day_before, day = gas_day_for(h0), gas_day_for(h1)
    assert day_before != day

    published: dict = {day_before: 0.050}

    async def fake_prices(start, end):
        hours, cursor = {}, start
        while cursor < end:
            price = published.get(gas_day_for(cursor))
            if price is not None:
                hours[cursor] = price
            cursor += timedelta(hours=1)
        return hours

    gas_prices = MagicMock()
    gas_prices.async_get_prices = AsyncMock(side_effect=fake_prices)
    coord = _coordinator(
        hass,
        {"gas_price_mode": "exchange", "gas_margin_eur_per_kwh": 0.005, "vat_percent": 24.0},
    )
    coord.attach_gas_price_client(gas_prices)
    coord._client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic=_gas_meter().eic,
                intervals=[
                    AccountingInterval(h0, 10.0, 0.0, 0.94, 0.0),
                    AccountingInterval(h1, 2.0, 0.0, 0.19, 0.0),
                    AccountingInterval(h2, 3.0, 0.0, 0.28, 0.0),
                ],
            )
        ]
    )

    async def tick():
        await coord._fetch_meter_window(
            _gas_meter(), h0, h2 + timedelta(hours=1), write_stats=True, force_start=False
        )
        await async_wait_recording_done(hass)
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            h0,
            h2 + timedelta(hours=1),
            {COST_ID},
            "hour",
            None,
            {"sum"},
        )
        return [round(r["sum"], 4) for r in stats[COST_ID]]

    # Only the earlier gas day is published: (0.050 + 0.005) * 1.24 * 10 kWh
    # = 0.682, and the newer hours carry the sum forward at zero cost.
    assert await tick() == [0.682, 0.682, 0.682]

    # The index arrives; the next tick re-prices the placeholders in place:
    # (0.060 + 0.005) * 1.24 = 0.0806 €/kWh for 2 and 3 kWh.
    published[day] = 0.060
    assert await tick() == [0.682, 0.8432, 1.085]


@pytest.mark.asyncio
async def test_exchange_cost_skipped_when_price_source_fails(hass):
    from custom_components.estfeed.gas_price import GasPriceError

    await _setup_recorder(hass)
    hour = _gas_day_boundary_hour()
    gas_prices = MagicMock()
    gas_prices.async_get_prices = AsyncMock(side_effect=GasPriceError("down"))
    coord = _coordinator(hass, {"gas_price_mode": "exchange"})
    coord.attach_gas_price_client(gas_prices)
    coord._client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic=_gas_meter().eic, intervals=[AccountingInterval(hour, 1.0, 0.0, 0.1, 0.0)]
            )
        ]
    )
    await coord._fetch_meter_window(
        _gas_meter(), hour, hour + timedelta(hours=1), write_stats=True, force_start=False
    )
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        hour,
        hour + timedelta(hours=1),
        {COST_ID},
        "hour",
        None,
        {"sum"},
    )
    assert COST_ID not in stats
    assert coord.last_gas_price_error == "down"


@pytest.mark.asyncio
async def test_consecutive_ticks_keep_sum_continuous(hass, monkeypatch):
    """Regression: the recorder's ``end`` is in seconds. Read as milliseconds,
    the resume point fell in 1970 and every tick rewrote its 30-day window
    chained onto the latest sum, inserting a jump the size of that window."""
    # The test recorder (HA 2025.1) predates the ``unit_class`` metadata key.
    monkeypatch.setattr("custom_components.estfeed.statistics._UNIT_CLASS_BY_UNIT", {})
    await _setup_recorder(hass)
    now_hour = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    first = now_hour - timedelta(days=3)
    intervals = [
        AccountingInterval(first + timedelta(hours=h), 1.0, 0.0, 0.1, 0.0) for h in range(48)
    ]
    coord = _coordinator(hass, {})

    async def tick(available: int):
        coord._client.get_metering_data = AsyncMock(
            return_value=[MeterData(eic=_gas_meter().eic, intervals=intervals[:available])]
        )
        await coord._fetch_meter_window(
            _gas_meter(),
            now_hour - timedelta(days=30),
            now_hour,
            write_stats=True,
            force_start=False,
        )
        await async_wait_recording_done(hass)

    await tick(24)
    await tick(48)
    sid = "estfeed:home_consumption_099g"
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, first, now_hour, {sid}, "hour", None, {"sum"}
    )
    sums = [round(r["sum"], 3) for r in stats[sid]]
    assert sums == [round(0.1 * (h + 1), 3) for h in range(48)]
