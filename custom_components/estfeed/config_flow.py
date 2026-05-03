"""Config flow for the Estfeed integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EstfeedAuthError, EstfeedClient, EstfeedError
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_RESOLUTION,
    DEFAULT_BACKFILL_MONTHS,
    DEFAULT_FRIENDLY_NAME,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
    Resolution,
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Required(CONF_FRIENDLY_NAME, default=DEFAULT_FRIENDLY_NAME): str,
    }
)


class EstfeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Estfeed."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step where the user enters API credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._validate(user_input)
            except EstfeedAuthError:
                errors["base"] = "invalid_auth"
            except EstfeedError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_FRIENDLY_NAME], data=user_input
                )

        return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)

    async def _validate(self, user_input: dict[str, Any]) -> None:
        session = async_get_clientsession(self.hass)
        client = EstfeedClient(
            session=session,
            client_id=user_input[CONF_CLIENT_ID],
            client_secret=user_input[CONF_CLIENT_SECRET],
        )
        end = datetime.now(tz=UTC)
        start = end - timedelta(days=7)
        await client.list_metering_points(start, end)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EstfeedOptionsFlow(config_entry)


class EstfeedOptionsFlow(OptionsFlow):
    """Handle Estfeed integration options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RESOLUTION,
                    default=current.get(CONF_RESOLUTION, Resolution.HOUR.value),
                ): vol.In([Resolution.HOUR.value, Resolution.QUARTER_HOUR.value]),
                vol.Required(
                    CONF_BACKFILL_MONTHS,
                    default=current.get(CONF_BACKFILL_MONTHS, DEFAULT_BACKFILL_MONTHS),
                ): vol.All(int, vol.Range(min=MIN_BACKFILL_MONTHS, max=MAX_BACKFILL_MONTHS)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
