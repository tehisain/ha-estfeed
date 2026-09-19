"""Pure pricing helpers: tariff math and cost row construction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.recorder.models import StatisticData

from .api import AccountingInterval, interval_value
from .const import Kind


def apply_tariff(
    spot_eur_per_kwh: float,
    vat_percent: float,
    margin_eur_per_kwh: float,
) -> float:
    """Apply VAT and a fixed per-kWh margin to a Nord Pool spot price.

    Formula: ``spot * (1 + vat_percent/100) + margin``. Negative margins are
    allowed (promotional discounts); negative spots are passed through (NPS
    occasionally settles negative).
    """
    return spot_eur_per_kwh * (1 + vat_percent / 100) + margin_eur_per_kwh


def make_tariff(
    vat_percent: float,
    margin_eur_per_kwh: float,
) -> Callable[[float], float]:
    """Curry apply_tariff so compute_cost_rows can call ``tariff(spot)``."""
    return lambda spot: apply_tariff(spot, vat_percent, margin_eur_per_kwh)


def compute_cost_rows(
    intervals: list[AccountingInterval],
    kind: Kind,
    prices: dict[datetime, float],
    tariff: Callable[[float], float],
    prior_sum: float,
) -> list[StatisticData]:
    """Build cumulative-sum cost rows from raw intervals and an hourly price map.

    Buckets intervals into hours (matching ``statistics.compute_statistic_rows``),
    multiplies each hour's summed kWh by ``tariff(prices[hour])``, and produces a
    running cumulative-sum series in EUR. Skips hours where the price is missing
    (NPS gap or future hour). Rounds to 4 decimal places (€0.0001).
    """
    hourly: dict[Any, float] = {}
    for ival in intervals:
        value = interval_value(ival, kind)
        if value is None:
            continue
        bucket = ival.period_start.replace(minute=0, second=0, microsecond=0)
        hourly[bucket] = hourly.get(bucket, 0.0) + float(value)

    return compute_cost_rows_from_hourly(hourly, prices, tariff, prior_sum)


def compute_cost_rows_from_hourly(
    hourly_energy: dict[datetime, float],
    prices: dict[datetime, float],
    tariff: Callable[[float], float],
    prior_sum: float,
) -> list[StatisticData]:
    """Build cumulative-sum cost rows from a per-hour energy map.

    ``hourly_energy`` maps top-of-hour UTC to the kWh consumed/produced that
    hour. Each hour's energy is multiplied by ``tariff(prices[hour])`` and
    accumulated into a running EUR series. Hours with no matching price (NPS
    gap or future hour) are skipped. Rounds to 4 decimals (€0.0001).

    Deriving cost from stored hourly energy (rather than re-fetched API
    intervals) keeps the cost statistic exactly consistent with the published
    consumption/production statistics it is meant to price.
    """
    rows: list[StatisticData] = []
    running = prior_sum
    for start in sorted(hourly_energy):
        price = prices.get(start)
        if price is None:
            continue
        cost = round(hourly_energy[start] * tariff(price), 4)
        running = round(running + cost, 4)
        rows.append({"start": start, "state": running, "sum": running})
    return rows
