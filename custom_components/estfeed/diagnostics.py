"""Diagnostics for the Estfeed integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    # Sample the cached NPS prices for the last 36 hours (top-of-hour UTC).
    # When cost statistics look wrong, comparing these values against
    # https://dashboard.elering.ee/api/nps/price quarter prices pinpoints
    # whether the bug is in the fetch path (mean mis-computed) or downstream.
    now_hour = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    sample_hours = [now_hour - timedelta(hours=h) for h in range(36)]
    nps_cache_recent = coordinator.nps_cache_snapshot(sample_hours)
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
            "baselines": {
                f"...REDACTED-{eic_suffix(eic)}|{kind.value}": {
                    "reset_at": b.reset_at.isoformat(),
                    "frozen_sum": b.frozen_sum,
                }
                for (eic, kind), b in coordinator.baselines.items()
            },
            "cumulative_since_reset": {
                f"...REDACTED-{eic_suffix(eic)}|{kind.value}": (
                    coordinator.cumulative_since_reset(eic, kind)
                )
                for (eic, kind) in coordinator.baselines
            },
            "cost_stream_ids": [
                cstream.statistic_id
                for m in coordinator.meters
                for cstream in coordinator.cost_streams_for(m)
            ],
            "last_nps_error": coordinator.last_nps_error,
            "nps_cache_size": coordinator.nps_cache_size,
            "nps_cache_recent": nps_cache_recent,
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
        "recent_requests": list(coordinator.recent_requests),
    }
