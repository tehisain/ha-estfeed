"""Tests for the Estfeed config flow."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.estfeed.api import (
    EstfeedAuthError,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    DOMAIN,
    CommodityType,
)


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


async def _setup_recorder(hass) -> None:
    """Set up the recorder so config-flow init doesn't fail on its dependency."""
    from homeassistant.components import recorder
    from homeassistant.helpers import recorder as recorder_helper

    with patch("homeassistant.components.recorder.ALLOW_IN_MEMORY_DB", True):
        if recorder.DOMAIN not in hass.data:
            recorder_helper.async_initialize_recorder(hass)
        assert await async_setup_component(
            hass,
            recorder.DOMAIN,
            {recorder.DOMAIN: {"db_url": "sqlite://", "commit_interval": 0}},
        )
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_user_step_happy_path(hass):
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(return_value=[_meter()]),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "cid",
                CONF_CLIENT_SECRET: "csec",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Home"
    assert result2["data"] == {
        CONF_CLIENT_ID: "cid",
        CONF_CLIENT_SECRET: "csec",
        CONF_FRIENDLY_NAME: "Home",
    }


@pytest.mark.asyncio
async def test_user_step_bad_credentials_shows_form_error(hass):
    await _setup_recorder(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.estfeed.config_flow.EstfeedClient.list_metering_points",
        new=AsyncMock(side_effect=EstfeedAuthError("bad")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "x",
                CONF_CLIENT_SECRET: "y",
                CONF_FRIENDLY_NAME: "Home",
            },
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
