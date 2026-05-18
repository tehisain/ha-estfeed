"""Estfeed Home Assistant integration."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import voluptuous as vol
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store

from .api import EstfeedClient, EstfeedError
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
)
from .coordinator import EstfeedCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

STORAGE_VERSION = 1

SERVICE_BACKFILL = "backfill_history"
SERVICE_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional("months", default=24): vol.All(
            cv.positive_int, vol.Range(min=MIN_BACKFILL_MONTHS, max=MAX_BACKFILL_MONTHS)
        ),
        vol.Optional("entry_id"): cv.string,
    }
)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "estfeed"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Estfeed from a config entry."""
    session = async_get_clientsession(hass)
    client = EstfeedClient(
        session=session,
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
    )
    slug = _slugify(entry.data.get(CONF_FRIENDLY_NAME, entry.title))

    end = datetime.now(tz=UTC)
    start = end - timedelta(days=7)
    try:
        meters = await client.list_metering_points(start, end)
    except EstfeedError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = EstfeedCoordinator(
        hass=hass, client=client, slug=slug, options={**entry.data, **entry.options}
    )
    coordinator.meters = meters
    coordinator.attach_store(Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.baselines"))
    await coordinator.async_load_baselines()

    needs_backfill = True
    recorder = get_instance(hass)
    for meter in meters:
        for stream in coordinator.streams_for(meter):
            existing = await recorder.async_add_executor_job(
                get_last_statistics, hass, 1, stream.statistic_id, True, {"sum"}
            )
            if existing.get(stream.statistic_id):
                needs_backfill = False
                break
        if not needs_backfill:
            break

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    if needs_backfill:
        hass.async_create_background_task(
            coordinator.async_initial_backfill(), name=f"{DOMAIN}_initial_backfill"
        )
    else:
        hass.async_create_background_task(
            coordinator.async_warm_cache(), name=f"{DOMAIN}_warm_cache"
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Estfeed config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.options = {**entry.data, **entry.options}


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _handle(call: ServiceCall) -> None:
        months = call.data.get("months", 24)
        entry_id = call.data.get("entry_id")
        targets = (
            [hass.data[DOMAIN][entry_id]]
            if entry_id and entry_id in hass.data[DOMAIN]
            else list(hass.data.get(DOMAIN, {}).values())
        )
        for coord in targets:
            coord.options = {**coord.options, CONF_BACKFILL_MONTHS: months}
            await coord.async_initial_backfill()

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle, schema=SERVICE_BACKFILL_SCHEMA)
