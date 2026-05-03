"""Estfeed binary_sensor platform: data freshness diagnostic."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MeteringPoint
from .const import ATTRIBUTION, DATA_FRESH_THRESHOLD, DOMAIN
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix


class DataFreshBinarySensor(CoordinatorEntity[EstfeedCoordinator], BinarySensorEntity):
    """ON if the newest cached interval is within DATA_FRESH_THRESHOLD."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "data_fresh"

    def __init__(self, coordinator: EstfeedCoordinator, meter: MeteringPoint) -> None:
        super().__init__(coordinator)
        self._meter = meter
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.slug}_data_fresh_{suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter.eic)},
            name=f"{self.coordinator.slug} ({self._meter.eic})",
            manufacturer="Elering Estfeed",
            model=self._meter.commodity_type.value,
        )

    @property
    def is_on(self) -> bool | None:
        all_intervals = [
            ival
            for (eic, _kind), bucket in self.coordinator.cache.items()
            if eic == self._meter.eic
            for ival in bucket
        ]
        newest = max((i.period_start for i in all_intervals), default=None)
        if newest is None:
            return None
        return (datetime.now(tz=UTC) - newest) <= DATA_FRESH_THRESHOLD


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DataFreshBinarySensor(coordinator, meter) for meter in coordinator.meters)
