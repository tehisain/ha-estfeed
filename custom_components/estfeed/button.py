"""Estfeed button platform: reset the cumulative-since-reset sensor."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import MeteringPoint
from .const import ATTRIBUTION, DOMAIN, Kind
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix


class CumulativeResetButton(ButtonEntity):
    """Captures the current cumulative sum as the new baseline."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: EstfeedCoordinator,
        meter: MeteringPoint,
        kind: Kind,
    ) -> None:
        self._coordinator = coordinator
        self._meter = meter
        self._kind = kind
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.slug}_{kind.value}_cumulative_reset_{suffix}"
        )
        self._attr_translation_key = f"{kind.value}_cumulative_reset"
        # Production reset mirrors the production cumulative sensor: disabled
        # by default for users who don't generate.
        if kind == Kind.PRODUCTION:
            self._attr_entity_registry_enabled_default = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter.eic)},
            name=f"{self._coordinator.slug} ({self._meter.eic})",
            manufacturer="Elering Estfeed",
            model=self._meter.commodity_type.value,
        )

    async def async_press(self) -> None:
        await self._coordinator.async_reset_cumulative(self._meter.eic, self._kind)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for meter in coordinator.meters:
        for kind in (Kind.CONSUMPTION, Kind.PRODUCTION):
            entities.append(CumulativeResetButton(coordinator, meter, kind))
    async_add_entities(entities)
