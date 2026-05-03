"""Tests for Estfeed diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.estfeed.api import MeteringPoint, Period
from custom_components.estfeed.const import CommodityType


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


@pytest.mark.asyncio
async def test_diagnostics_redacts_secrets_and_eic_body(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.estfeed import async_setup_entry
    from custom_components.estfeed.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_FRIENDLY_NAME,
        DOMAIN,
    )
    from custom_components.estfeed.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CLIENT_ID: "client-id-secret",
            CONF_CLIENT_SECRET: "client-secret-value",
            CONF_FRIENDLY_NAME: "Home",
        },
        options={},
        unique_id="client-id-secret",
    )
    entry.add_to_hass(hass)

    fake_recorder = MagicMock()

    async def _exec(func, *args, **kwargs):
        return func(*args, **kwargs)

    fake_recorder.async_add_executor_job = _exec

    with (
        patch(
            "custom_components.estfeed.EstfeedClient.list_metering_points",
            new=AsyncMock(return_value=[_meter()]),
        ),
        patch(
            "custom_components.estfeed.get_instance",
            return_value=fake_recorder,
        ),
        patch(
            "custom_components.estfeed.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.EstfeedCoordinator.async_initial_backfill",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.estfeed.EstfeedCoordinator.async_warm_cache",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.estfeed.EstfeedCoordinator.async_config_entry_first_refresh",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ),
    ):
        assert await async_setup_entry(hass, entry)

        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_CLIENT_SECRET] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_CLIENT_ID] == "**REDACTED**"
    assert diag["meters"][0]["eic"].endswith("089N")
    assert "38ZEE" not in diag["meters"][0]["eic"]
