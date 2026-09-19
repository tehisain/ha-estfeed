"""Estfeed Home Assistant integration."""

from __future__ import annotations

import logging
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
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_VAT_PERCENT,
    DOMAIN,
    MAX_BACKFILL_MONTHS,
    MIN_BACKFILL_MONTHS,
    CommodityType,
)
from .coordinator import EstfeedCoordinator
from .nps import EleringNpsClient
from .utils import slugify

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

SERVICE_SET_CUMULATIVE_RESET_AT = "set_cumulative_reset_at"
SERVICE_SET_CUMULATIVE_RESET_AT_SCHEMA = vol.Schema(
    {
        vol.Required("reset_at"): cv.datetime,
        vol.Optional("entry_id"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Estfeed from a config entry."""
    session = async_get_clientsession(hass)
    client = EstfeedClient(
        session=session,
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
    )
    slug = slugify(entry.data.get(CONF_FRIENDLY_NAME, entry.title))

    end = datetime.now(tz=UTC)
    start = end - timedelta(days=7)
    try:
        meters = await client.list_metering_points(start, end)
    except EstfeedError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug=slug,
        options={**entry.data, **entry.options},
        config_entry=entry,
    )
    coordinator.meters = meters
    coordinator.attach_nps_client(EleringNpsClient(session))
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

    needs_cost_backfill = False
    for meter in meters:
        if meter.commodity_type != CommodityType.ELECTRICITY:
            continue
        for cstream in coordinator.cost_streams_for(meter):
            existing = await recorder.async_add_executor_job(
                get_last_statistics, hass, 1, cstream.statistic_id, True, {"sum"}
            )
            if not existing.get(cstream.statistic_id):
                needs_cost_backfill = True
                break
        if needs_cost_backfill:
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
        if needs_cost_backfill:
            # Energy stats already exist; only cost is missing — fill cost
            # without rewriting the (correct) energy series.
            hass.async_create_background_task(
                coordinator.async_rebuild_cost(), name=f"{DOMAIN}_cost_initial_fill"
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
    old_options = dict(coordinator.options)
    new_options = {**entry.data, **entry.options}
    coordinator.options = new_options

    if old_options.get(CONF_VAT_PERCENT) != new_options.get(CONF_VAT_PERCENT) or old_options.get(
        CONF_MARGIN_EUR_PER_KWH
    ) != new_options.get(CONF_MARGIN_EUR_PER_KWH):
        hass.async_create_background_task(
            coordinator.async_rebuild_cost(), name=f"{DOMAIN}_cost_rebuild"
        )


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
            # Pass months explicitly instead of mutating coord.options —
            # an in-memory override would be silently reverted the next
            # time the options flow saves.
            await coord.async_initial_backfill(months=months)

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, _handle, schema=SERVICE_BACKFILL_SCHEMA)

    async def _set_reset_at(call: ServiceCall) -> None:
        reset_at = call.data["reset_at"]
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=UTC)
        entry_id = call.data.get("entry_id")
        targets = (
            [hass.data[DOMAIN][entry_id]]
            if entry_id and entry_id in hass.data[DOMAIN]
            else list(hass.data.get(DOMAIN, {}).values())
        )
        for coord in targets:
            # Restore both consumption and production baselines so the
            # anchor rewind is symmetric with how baselines are captured.
            await coord.async_set_cumulative_reset_at(reset_at)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CUMULATIVE_RESET_AT,
        _set_reset_at,
        schema=SERVICE_SET_CUMULATIVE_RESET_AT_SCHEMA,
    )
