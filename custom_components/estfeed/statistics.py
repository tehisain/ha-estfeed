"""Statistics helpers for Estfeed: build statistic_ids, compute cumulative sums."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from .api import AccountingInterval
from .const import DOMAIN, Kind


class StatisticRow(TypedDict):
    """One row to pass to async_add_external_statistics."""

    start: datetime
    state: float
    sum: float


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
) -> list[StatisticRow]:
    """Build cumulative-sum statistic rows from raw intervals.

    Skips intervals where the relevant value is None. Output is sorted by start
    ascending. `state` is set equal to `sum` (counter semantics for HA's
    statistics display).
    """
    sorted_ivals = sorted(intervals, key=lambda i: i.period_start)
    rows: list[StatisticRow] = []
    running = prior_sum
    for ival in sorted_ivals:
        value = _interval_value(ival, kind)
        if value is None:
            continue
        running += float(value)
        rows.append({"start": ival.period_start, "state": running, "sum": running})
    return rows
