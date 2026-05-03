"""Estfeed sensor entities and lagging-period helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AccountingInterval, MeteringPoint
from .const import ATTRIBUTION, DOMAIN, CommodityType, Kind
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix

if TYPE_CHECKING:
    from collections import deque


class LaggingPeriod(StrEnum):
    YESTERDAY = "yesterday"
    MONTH_TO_DATE = "month_to_date"
    PREVIOUS_MONTH = "previous_month"


def window_for_period(
    period: LaggingPeriod, *, now: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return [start, end) for the given period in the given tz."""
    local_now = now.astimezone(tz)
    today_local = datetime(local_now.year, local_now.month, local_now.day, tzinfo=tz)
    if period == LaggingPeriod.YESTERDAY:
        return today_local - timedelta(days=1), today_local
    if period == LaggingPeriod.MONTH_TO_DATE:
        month_start = today_local.replace(day=1)
        return month_start, local_now
    if period == LaggingPeriod.PREVIOUS_MONTH:
        first_of_this_month = today_local.replace(day=1)
        last_month_end = first_of_this_month
        last_month_start = (first_of_this_month - timedelta(days=1)).replace(day=1)
        return last_month_start, last_month_end
    raise ValueError(f"unknown period: {period}")


def _interval_value(ival: AccountingInterval, kind: Kind) -> float | None:
    if kind == Kind.CONSUMPTION:
        return ival.consumption_kwh if ival.consumption_kwh is not None else ival.consumption_m3
    return ival.production_kwh if ival.production_kwh is not None else ival.production_m3


def sum_for_period(
    intervals: list[AccountingInterval] | deque[AccountingInterval],
    kind: Kind,
    period: LaggingPeriod,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> float:
    """Sum kWh/m³ values that fall within the given local period."""
    start_local, end_local = window_for_period(period, now=now, tz=tz)
    start_utc = start_local.astimezone(now.tzinfo or ZoneInfo("UTC"))
    end_utc = end_local.astimezone(now.tzinfo or ZoneInfo("UTC"))
    total = 0.0
    for ival in intervals:
        if start_utc <= ival.period_start < end_utc:
            v = _interval_value(ival, kind)
            if v is not None:
                total += float(v)
    return total


class _EstfeedEntity(CoordinatorEntity[EstfeedCoordinator]):
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: EstfeedCoordinator, meter: MeteringPoint) -> None:
        super().__init__(coordinator)
        self._meter = meter

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter.eic)},
            name=f"{self.coordinator.slug} ({self._meter.eic})",
            manufacturer="Elering Estfeed",
            model=self._meter.commodity_type.value,
        )


class LaggingSensor(_EstfeedEntity, SensorEntity):
    """A consumption/production total over yesterday / MTD / previous month."""

    def __init__(
        self,
        coordinator: EstfeedCoordinator,
        meter: MeteringPoint,
        kind: Kind,
        period: LaggingPeriod,
        multi_meter: bool,
    ) -> None:
        super().__init__(coordinator, meter)
        self._kind = kind
        self._period = period
        self._multi_meter = multi_meter
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.slug}_{kind.value}_{period.value}_{suffix}"
        self._attr_translation_key = f"{kind.value}_{period.value}"
        self._attr_device_class = SensorDeviceClass.ENERGY
        if meter.commodity_type == CommodityType.ELECTRICITY:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        else:
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_native_unit_of_measurement = "m³"
        # Production sensors disabled by default (most users don't generate).
        if kind == Kind.PRODUCTION:
            self._attr_entity_registry_enabled_default = False

    @property
    def _bucket(self) -> list[AccountingInterval]:
        return list(self.coordinator.cache.get((self._meter.eic, self._kind), []))

    @property
    def available(self) -> bool:
        return bool(self._bucket)

    @property
    def native_value(self) -> float:
        bucket = self._bucket
        tz = ZoneInfo(self.coordinator.hass.config.time_zone or "UTC")
        now = datetime.now(tz=UTC)
        return round(sum_for_period(bucket, self._kind, self._period, now=now, tz=tz), 3)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        tz = ZoneInfo(self.coordinator.hass.config.time_zone or "UTC")
        now = datetime.now(tz=UTC)
        start, end = window_for_period(self._period, now=now, tz=tz)
        as_of = max((i.period_start for i in self._bucket), default=None)
        return {
            "meter_eic": self._meter.eic,
            "commodity_type": self._meter.commodity_type.value,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "as_of": as_of.isoformat() if as_of else "",
        }


class LatestIntervalSensor(_EstfeedEntity, SensorEntity):
    """Diagnostic: timestamp of the newest cached interval."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "latest_interval"

    def __init__(self, coordinator: EstfeedCoordinator, meter: MeteringPoint) -> None:
        super().__init__(coordinator, meter)
        suffix = eic_suffix(meter.eic)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.slug}_latest_interval_{suffix}"

    @property
    def native_value(self) -> datetime | None:
        all_intervals = [
            ival
            for (eic, _kind), bucket in self.coordinator.cache.items()
            if eic == self._meter.eic
            for ival in bucket
        ]
        return max((i.period_start for i in all_intervals), default=None)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensor entities for each meter."""
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    multi_meter = len(coordinator.meters) > 1
    entities: list[SensorEntity] = []
    for meter in coordinator.meters:
        for kind in (Kind.CONSUMPTION, Kind.PRODUCTION):
            for period in LaggingPeriod:
                entities.append(LaggingSensor(coordinator, meter, kind, period, multi_meter))
        entities.append(LatestIntervalSensor(coordinator, meter))
    async_add_entities(entities)
