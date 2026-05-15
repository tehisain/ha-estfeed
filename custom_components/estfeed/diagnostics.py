"""Diagnostics for the Estfeed integration."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import AccountingInterval
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN, Kind
from .coordinator import EstfeedCoordinator
from .sensor import LaggingPeriod, sum_for_period, window_for_period
from .statistics import eic_suffix

_REDACT_KEYS = {CONF_CLIENT_ID, CONF_CLIENT_SECRET}


def _cache_dump(intervals: list[AccountingInterval], kind: Kind) -> dict[str, Any]:
    """Bucket cache intervals by UTC day with count + sum of values.

    Temporary diagnostic to investigate a cache/statistics divergence where the
    lagging-period sensor sums to a smaller total than long-term statistics for
    the same window.
    """
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "sum": 0.0})
    none_count = 0
    for ival in intervals:
        day = ival.period_start.strftime("%Y-%m-%d")
        by_day[day]["count"] += 1
        if kind == Kind.CONSUMPTION:
            v = ival.consumption_kwh if ival.consumption_kwh is not None else ival.consumption_m3
        else:
            v = ival.production_kwh if ival.production_kwh is not None else ival.production_m3
        if v is None:
            none_count += 1
        else:
            by_day[day]["sum"] += float(v)
    sorted_days = sorted(by_day.keys())
    return {
        "total_count": len(intervals),
        "none_count": none_count,
        "min_period_start": intervals[0].period_start.isoformat() if intervals else None,
        "max_period_start": intervals[-1].period_start.isoformat() if intervals else None,
        "by_utc_day": {
            d: {"count": int(by_day[d]["count"]), "sum": round(by_day[d]["sum"], 3)}
            for d in sorted_days
        },
    }


def _window_probe(
    intervals: list[AccountingInterval],
    kind: Kind,
    period: LaggingPeriod,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> dict[str, Any]:
    """Replay sum_for_period and report what it sees, for divergence debugging."""
    start_local, end_local = window_for_period(period, now=now, tz=tz)
    start_utc = start_local.astimezone(now.tzinfo or UTC)
    end_utc = end_local.astimezone(now.tzinfo or UTC)
    in_window = [i for i in intervals if start_utc <= i.period_start < end_utc]
    raw_sum = sum_for_period(intervals, kind, period, now=now, tz=tz)
    none_in_window = 0
    sample_starts = []
    for i in in_window[:3]:
        sample_starts.append(i.period_start.isoformat())
        if (i.consumption_kwh if kind == Kind.CONSUMPTION else i.production_kwh) is None:
            none_in_window += 1
    return {
        "window_start_utc": start_utc.isoformat(),
        "window_end_utc": end_utc.isoformat(),
        "now_utc": now.isoformat(),
        "tz_name": str(tz),
        "in_window_count": len(in_window),
        "in_window_sum": round(raw_sum, 3),
        "sample_first_starts": sample_starts,
        "sample_last_starts": [i.period_start.isoformat() for i in in_window[-3:]],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EstfeedCoordinator = hass.data[DOMAIN][entry.entry_id]
    tz = ZoneInfo(hass.config.time_zone or "UTC")
    now = datetime.now(tz=UTC)
    window_probes = {}
    for m in coordinator.meters:
        for kind in (Kind.CONSUMPTION, Kind.PRODUCTION):
            bucket = list(coordinator.cache.get((m.eic, kind), []))
            for period in LaggingPeriod:
                key = f"{eic_suffix(m.eic)}_{kind.value}_{period.value}"
                window_probes[key] = _window_probe(bucket, kind, period, now=now, tz=tz)
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
            "cache_dump": {
                f"{eic_suffix(m.eic)}_{kind.value}": _cache_dump(
                    list(coordinator.cache.get((m.eic, kind), [])), kind
                )
                for m in coordinator.meters
                for kind in (Kind.CONSUMPTION, Kind.PRODUCTION)
            },
            "window_probes": window_probes,
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
