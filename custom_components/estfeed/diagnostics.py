"""Diagnostics for the Estfeed integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN
from .coordinator import EstfeedCoordinator
from .statistics import eic_suffix

_REDACT_KEYS = {CONF_CLIENT_ID, CONF_CLIENT_SECRET}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            "title": entry.title,
            "options": dict(entry.options),
            "data": async_redact_data(dict(entry.data), _REDACT_KEYS),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": str(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "intervals_cached_per_meter": {
                eic: sum(len(b) for (e, _k), b in coordinator.cache.items() if e == eic)
                for eic in {m.eic for m in coordinator.meters}
            },
            "last_meter_errors": dict(coordinator.last_meter_errors),
        },
        "meters": [
            {
                "eic": f"...REDACTED-{eic_suffix(m.eic)}",
                "commodity_type": m.commodity_type.value,
                "validity_periods": [
                    {"from": p.start.isoformat(), "to": p.end.isoformat() if p.end else None}
                    for p in m.periods
                ],
            }
            for m in coordinator.meters
        ],
        "recent_requests": list(coordinator._client.recent_requests),
    }
