"""Statistics helpers for Estfeed: build statistic_ids, compute cumulative sums."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant

from .api import AccountingInterval
from .const import DOMAIN, Kind

# Public alias kept for backwards compatibility with callers / tests that import
# StatisticRow from this module. The recorder's StatisticData TypedDict already
# allows start/state/sum (plus optional fields), so we use it directly.
StatisticRow = StatisticData


def eic_suffix(eic: str) -> str:
    """Last 4 alphanumeric characters of an EIC, no hyphens, used to disambiguate."""
    return "".join(ch for ch in eic if ch.isalnum())[-4:]


def build_statistic_id(slug: str, kind: Kind, suffix: str, *, multi_meter: bool) -> str:
    """Construct a deterministic statistic_id for a (slug, kind, eic) combination."""
    # multi_meter is currently informational; the suffix is always included to keep
    # statistic_ids stable if the user later adds a second meter to the same entry.
    del multi_meter
    return f"{DOMAIN}:{slug}_{kind.value}_{suffix}"


def _interval_value(interval: AccountingInterval, kind: Kind) -> float | None:
    if kind == Kind.CONSUMPTION:
        return (
            interval.consumption_kwh
            if interval.consumption_kwh is not None
            else interval.consumption_m3
        )
    return (
        interval.production_kwh if interval.production_kwh is not None else interval.production_m3
    )


def compute_statistic_rows(
    intervals: list[AccountingInterval],
    kind: Kind,
    prior_sum: float,
) -> list[StatisticData]:
    """Build cumulative-sum statistic rows from raw intervals.

    Skips intervals where the relevant value is None. Output is sorted by start
    ascending. `state` is set equal to `sum` (counter semantics for HA's
    statistics display).
    """
    sorted_ivals = sorted(intervals, key=lambda i: i.period_start)
    rows: list[StatisticData] = []
    running = prior_sum
    for ival in sorted_ivals:
        value = _interval_value(ival, kind)
        if value is None:
            continue
        running += float(value)
        rows.append({"start": ival.period_start, "state": running, "sum": running})
    return rows


@dataclass(frozen=True, slots=True)
class StatisticStream:
    """Identifies one external statistics stream (one statistic_id)."""

    statistic_id: str
    name: str
    unit: str
    kind: Kind


async def async_write_meter_statistics(
    hass: HomeAssistant,
    stream: StatisticStream,
    intervals: list[AccountingInterval],
    prior_sum: float,
) -> float:
    """Compute and submit external statistics rows for one meter+kind.

    Returns the running cumulative sum after processing these intervals: equal
    to ``prior_sum`` when no rows are produced, otherwise the ``sum`` of the
    last (latest) row. Callers fetching multiple chunks should pass this back
    in as ``prior_sum`` for the next chunk to avoid a read-after-write hazard
    against HA's recorder (which may not flush statistics writes synchronously).
    """
    rows = compute_statistic_rows(intervals, stream.kind, prior_sum=prior_sum)
    if not rows:
        return prior_sum
    metadata: StatisticMetaData = {
        "source": DOMAIN,
        "statistic_id": stream.statistic_id,
        "name": stream.name,
        "unit_of_measurement": stream.unit,
        "has_sum": True,
        "has_mean": False,
    }
    # async_add_external_statistics is a synchronous @callback in this HA version
    # (inspect.iscoroutinefunction returned False); no await needed.
    async_add_external_statistics(hass, metadata, rows)
    last_sum = rows[-1].get("sum")
    return float(last_sum) if last_sum is not None else prior_sum
